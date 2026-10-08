# HANDOFF — SEC iDisc Data → efin MCP (C# port spec)

เอกสารส่งมอบสำหรับทีมที่จะ **เขียนใหม่เป็น C#** เพื่อรวมเข้า efin MCP

Python ใน repo นี้คือ **reference implementation** ที่ทำงานจริงบน production แล้ว ใช้เป็นต้นแบบ
และใช้ตรวจคำตอบว่า port ถูกหรือไม่ (ดู §9 Test Vectors)

| | |
|---|---|
| Reference (Python) | https://idisc-data-production.up.railway.app — `/docs` มี OpenAPI |
| Source | repo นี้ (`app/`) |
| ต้นทางข้อมูล | `https://market.sec.or.th` (เว็บ ก.ล.ต. — **scrape HTML ไม่มี public API**) |
| ครอบคลุม | หุ้นไทยทุกตัว SET + mai = **866 บริษัท** (863 ตัวมี Form 59) |

> ⚠️ **อ่าน §3 ก่อนเขียนโค้ด** — ต้นทางมี bot protection, encoding trap และ parameter ที่ถ้าพลาดจะได้ข้อมูลผิดแบบเงียบ ๆ (ไม่ error) ทุกข้อในนั้นเจอจากการลองจริง ไม่ใช่ทฤษฎี

---

## 1. ขอบเขตงาน

ต้อง implement 2 ชุดข้อมูล:

| # | ชุดข้อมูล | ต้นทาง | ผลลัพธ์ |
|---|---|---|---|
| 1 | **แบบ 59** — รายงานการเปลี่ยนแปลงการถือหลักทรัพย์ของผู้บริหาร | `/public/idisc/th/Viewmore/r59-2` | records + analytics |
| 2 | **Sustainability** — CG Score, AGM Level, Thai-CAC, SET ESG | `/public/idisc/th/CompanyProfile/Listed/{symbol}` | 4 ตัวชี้วัด + scorecard |

มี layer เสริมที่ต้องมีด้วย:

3. **Symbol registry** — แปลง ticker (`GULF`) → `uniqueIDReference` (`0000008616`) เพราะ Form 59 ค้นด้วยรหัสภายใน ไม่ใช่ชื่อย่อ
4. **Rate limiter + cache** — บังคับ ไม่ใช่ optional (§3.4)

---

## 2. สถาปัตยกรรม (แนะนำให้ port ตามนี้)

```
Controllers/          REST endpoints (ถ้าต้องการ) + MCP tools
  ↓
Services/
  SymbolRegistry      ticker → uniqueIDReference (2 ทาง + cache 24h)
  Form59Service       fetch + parse + analytics
  SustainabilityService
  ↓
Infrastructure/
  SecHttpClient       rate limiter + retry + bot-challenge detection
  Mappings            ตาราง vocab ไทย → English code (§7)
```

**กฎข้อเดียวที่ห้ามพลาด:** `SecHttpClient` ต้องเป็น **singleton เดียวทั้ง process** และ rate limiter ต้องเป็น global
ถ้าแต่ละ service สร้าง HttpClient เอง จะยิงเว็บ ก.ล.ต. เกิน limit แล้วโดนบล็อก (§3.4)

---

## 3. Upstream contract — เว็บ ก.ล.ต. (สำคัญที่สุด)

### 3.1 Endpoints

```
GET /public/idisc/th/Viewmore/r59-2?DateFrom=&DateTo=&DateType=&uniqueIDReference=
GET /public/idisc/th/CompanyProfile/Listed/{SYMBOL}
GET /public/idisc/th/company/listed/{LETTER}        LETTER ∈ { 2, 8, A..Z }
```

### 3.2 ⚠️ `DateType` เป็น parameter บังคับ

**ถ้าไม่ส่ง `DateType` เว็บจะ "เมินช่วงวันที่ทั้งหมด" แล้วคืนทุก record ที่เคยมี — โดยไม่ error**

พิสูจน์แล้วกับ PTT (`0000001074`) ช่วง `20250101`–`20251231`:

| ส่ง | ผลลัพธ์ |
|---|---|
| `DateType=1` | **1 record** (ถูกต้อง) |
| `DateType=2` หรือ `3` | 1 record (เหมือนกัน) |
| **ไม่ส่งเลย** | **49 records** ← ข้อมูลทั้งหมดตั้งแต่ต้น |

ค่า 1/2/3 ให้ผลเหมือนกันทุกกรณีที่ทดสอบ → **ส่ง `DateType=1` เสมอ**

รูปแบบวันที่ใน query = **`yyyyMMdd` ค.ศ.** (ไม่ใช่ พ.ศ.)

### 3.3 ⚠️ Encoding — UTF-8 แต่ไม่ประกาศ charset

หน้าเว็บเป็น UTF-8 แต่ **ไม่มี `<meta charset>`** → HttpClient ที่เดา encoding เองจะได้ภาษาไทยเพี้ยน

```csharp
// ✅ บังคับ UTF-8 เสมอ
var bytes = await response.Content.ReadAsByteArrayAsync(ct);
var html  = Encoding.UTF8.GetString(bytes);

// ❌ อย่าใช้ — จะ fallback เป็น ISO-8859-1 แล้วไทยพัง
var html = await response.Content.ReadAsStringAsync(ct);
```

### 3.4 ⚠️ Bot protection (F5/Shape) — เรื่องใหญ่ที่สุด

เว็บ ก.ล.ต. มี bot defence ถ้ายิงเร็วเกินจะคืน **หน้า JavaScript challenge แทนข้อมูล พร้อม HTTP 200**

**เจอมาแล้วจริง:** ยิง index A–Z 28 หน้าพร้อมกัน → โดนบล็อกทันที และ**บล็อกต่ออีกหลายนาที** แม้ยิงทีละ request เดียว

**วิธีตรวจจับ** — หน้า challenge จะมี string `bobcmn`:

```csharp
static bool IsBotChallenge(string html)
{
    if (html.Contains("bobcmn", StringComparison.Ordinal)) return true;
    // สำรอง: หน้าจริงทุกหน้ามี aspnetForm/divBody เสมอ
    if (html.Length < 12000
        && !html.Contains("aspnetForm") && !html.Contains("divBody"))
        return html.Contains("<script", StringComparison.OrdinalIgnoreCase);
    return false;
}
```

**สิ่งที่ต้องทำ:**

| กฎ | ค่า | เหตุผล |
|---|---|---|
| เว้นระยะระหว่าง request | **≥ 1.0 วินาที** (global) | ค่าที่ทดสอบแล้วปลอดภัย |
| ยิงพร้อมกันสูงสุด | **2** | เกินกว่านี้เสี่ยง |
| crawl A–Z | **sequential เท่านั้น** + abort ทันทีถ้าเจอ challenge | ~28 วิ แต่ cache 24 ชม. |
| เจอ challenge | **ล้าง cookie** + backoff ยาว (5s × 2^n) | cookie ที่ติด challenge จะพังต่อเนื่อง |
| ตอบ client | **503 + `Retry-After`** | **ห้ามตอบ 404** — ข้อมูลมีอยู่ แค่ถูกกัน |

```csharp
// Rate limiter: ต้องเป็น singleton
private readonly SemaphoreSlim _gate = new(2);          // max concurrent
private readonly SemaphoreSlim _spacer = new(1);
private DateTime _last = DateTime.MinValue;

async Task<T> ThrottledAsync<T>(Func<Task<T>> work, CancellationToken ct)
{
    await _gate.WaitAsync(ct);
    try
    {
        await _spacer.WaitAsync(ct);
        try
        {
            var wait = _last.AddSeconds(1.0) - DateTime.UtcNow;
            if (wait > TimeSpan.Zero) await Task.Delay(wait, ct);
            _last = DateTime.UtcNow;
        }
        finally { _spacer.Release(); }
        return await work();
    }
    finally { _gate.Release(); }
}
```

**Headers ที่ต้องส่ง** — ถ้าไม่ส่ง User-Agent จะถูกตัดการเชื่อมต่อตั้งแต่ TLS handshake:

```
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36
Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8
Accept-Language: th,en-US;q=0.9,en;q=0.8
```

### 3.5 ไม่มี pagination

ตารางผลลัพธ์ render ทุกแถวในหน้าเดียว ทดสอบถึง **233 แถว** (EA ช่วง 2015–2026) ยังครบ ไม่มี row cap
→ ไม่ต้องทำ paging

### 3.6 ตรวจความครบถ้วนของ parse

หัวตารางบอกจำนวนแถวที่ควรได้:

```
ข้อมูลแบบรายงาน... (จำนวนรายการที่พบ 29 รายการ)
```

```csharp
// regex: จำนวนรายการที่พบ\s*([\d,]+)\s*รายการ
```

**ต้องเทียบกับจำนวนที่ parse ได้จริงทุกครั้ง** แล้วส่งออกเป็น `parseComplete` — ถ้าไม่ตรงแปลว่าเว็บเปลี่ยนโครงสร้าง ควร log warning (อย่า fail เงียบ)

---

## 4. Symbol registry — ticker → uniqueIDReference

Form 59 ค้นด้วยรหัส 10 หลัก ไม่ใช่ ticker มี 2 ทาง:

**ทาง 1 (เร็ว, 1 request)** — หน้า CompanyProfile ฝังรหัสไว้ในลิงก์ของตัวเอง:

```
/public/idisc/th/FinancialReport/ALLMIXED-0000008616/...?symbol=GULF
```

```csharp
// regex ใช้ได้กับทุกแบบลิงก์ที่หน้านั้นมี
new Regex(@"(?:ALLMIXED|FS|R561|R562|ALL)-(\d{10})")
```

**ทาง 2 (bulk, cache 24h)** — crawl index A–Z ได้ทั้ง 866 บริษัท พร้อมชื่อ/ตลาด/หมวด
ใช้เป็น fallback และสำหรับ endpoint ค้นรายชื่อ

```
ตาราง id="gcompany_{LETTER}"
คอลัมน์: ชื่อย่อ | ชื่อบริษัท(ลิงก์) | Filing | งบการเงิน(มี FS-xxxxxxxxxx) | 56-1 | 56-2 | Ranking
Ranking link → /Ranking/Listed/Sector/SET-ENERG  →  market=SET, sector=ENERG
```

⚠️ **3 บริษัท (`POWER`, `TBSP`, `ASTR`) มีหน้า profile แต่ไม่มีรหัสเลย** → Form 59 เรียกไม่ได้จริง ๆ
ต้องตอบ **404 พร้อมบอกเหตุผลชัดเจน** (ไม่ใช่ "ไม่พบบริษัท") และ **Sustainability ยังเรียกได้ปกติ**

---

## 5. Parsing spec — แบบ 59

### 5.1 ตาราง

`<table id="gPP09T01">` — 9 คอลัมน์ ตามลำดับ:

| # | หัวตาราง | field |
|---|---|---|
| 0 | ชื่อบริษัท | `companyLabelRaw` |
| 1 | ชื่อผู้บริหาร | `executiveName` |
| 2 | ความสัมพันธ์ * | `relationship` |
| 3 | ประเภทหลักทรัพย์ | `securityType` |
| 4 | วันที่ได้มา/จำหน่าย | `transactionDate` |
| 5 | จำนวน | `shares` |
| 6 | ราคา | `pricePerShare` |
| 7 | วิธีการได้มา/จำหน่าย | `method` |
| 8 | หมายเหตุ | `reportUrl` (ลิงก์) |

แนะนำ: map คอลัมน์จาก **ข้อความหัวตาราง** ก่อน แล้ว fallback เป็นลำดับ — เผื่อเว็บสลับคอลัมน์

### 5.2 ⚠️ "ชื่อผู้บริหาร" ≠ คนที่ซื้อขาย

นี่คือจุดที่พลาดง่ายที่สุดและทำให้ attribute ผิดคน

- คอลัมน์ **ชื่อผู้บริหาร** = ผู้บริหารที่**มีหน้าที่รายงาน**
- คอลัมน์ **ความสัมพันธ์** = **ใครเป็นคนที่การถือครองเปลี่ยน** เทียบกับผู้บริหารคนนั้น

```
ชื่อผู้บริหาร = "นาย สารัชถ์ รัตนาวะดี"
ความสัมพันธ์  = "นิติบุคคลซึ่งผู้จัดทำรายงาน ... (บริษัท กัลฟ์ โฮลดิ้งส์ (ประเทศไทย) จำกัด)"
→ คนที่ซื้อจริงคือ "บริษัท กัลฟ์ โฮลดิ้งส์" ไม่ใช่คุณสารัชถ์
```

**กฎ:**
```
holderName = ข้อความในวงเล็บท้ายสุด (ถ้ามี)
           : ไม่มี → ใช้ executiveName
holderIsExecutive = (ไม่มีวงเล็บ)
```

ต้อง output **ทั้ง 2 มุมมอง**: `byHolder` (ใครสะสม/ขายจริง) และ `byExecutive` (ตรงกับที่เว็บแสดง)

### 5.3 ⚠️ วงเล็บซ้อน — regex ธรรมดาใช้ไม่ได้

```
(บริษัท กัลฟ์ โฮลดิ้งส์ (ประเทศไทย) จำกัด)
```

`\(([^()]+)\)$` จะ match ไม่ได้ ต้องไล่นับวงเล็บถอยหลังจากตัวปิดตัวสุดท้าย:

```csharp
static string? TrailingParenthetical(string text)
{
    var s = text.TrimEnd();
    if (!s.EndsWith(")")) return null;
    int depth = 0;
    for (int i = s.Length - 1; i >= 0; i--)
    {
        if (s[i] == ')') depth++;
        else if (s[i] == '(' && --depth == 0)
            return s.Substring(i + 1, s.Length - i - 2).Trim();
    }
    return null;
}
```

### 5.4 ⚠️ ลำดับการ match ความสัมพันธ์ — ห้ามสลับ

ข้อความ **นิติบุคคล** มีคำของประเภทอื่นอยู่ข้างใน:

> "**นิติบุคคล**ซึ่ง**ผู้จัดทำรายงาน** **คู่สมรส**หรือผู้ที่อยู่กินด้วยกันฉันสามีภริยา และ**บุตรที่ยังไม่บรรลุนิติภาวะ** ถือหุ้นรวมกันเกินร้อยละ 30..."

ถ้า match `คู่สมรส` ก่อน จะกลายเป็นคู่สมรสซื้อ ทั้งที่จริงเป็นบริษัทซื้อ
**ต้องเช็ค `นิติบุคคล` เป็นอันดับแรกเสมอ** (ดูตารางลำดับใน §7.3)

### 5.5 ⚠️ รายการที่ถูกยกเลิก (Revoked)

ก.ล.ต. **ไม่ลบ** filing ที่ผู้รายงานยกเลิก แต่ขีดฆ่าไว้:

```html
<td><span style="text-decoration: line-through">429,000</span><br/>Revoked by Reporter</td>
```

`InnerText` จะได้ `"429,000Revoked by Reporter"` → parse ตัวเลขไม่ผ่าน → ถ้าไม่จัดการ จะได้ `shares = null`
**แต่แถวยังถูกนับเป็นการซื้อจริงที่มีราคา** ← ข้อมูลผิด

พบจริง **19 จาก 1,090 แถว (1.7%)** ใน 6 บริษัท — ข้อความกำกับมีแบบเดียว: `Revoked by Reporter`

**วิธีจัดการ:**
```csharp
var struck = cell.QuerySelector("span[style*='line-through']");
bool isRevoked = struck != null;
// อ่านตัวเลขจาก struck.TextContent (ไม่ใช่ cell.TextContent)
// isRevoked → sharesSigned = null  และ ตัดออกจาก analytics ทั้งหมด
```
**ห้ามลบแถวทิ้ง** — คงไว้พร้อม flag `isRevoked` + `revocationNote`

### 5.6 วันที่ พ.ศ. → ค.ศ.

เว็บแสดง `27/02/2566` (DD/MM/BBBB พ.ศ.) ต้องแปลงเป็น ISO `2023-02-27`

```csharp
// ปี >= 2200 ถือว่าเป็น พ.ศ. → ลบ 543
// (กันกรณีอนาคตเว็บเปลี่ยนเป็น ค.ศ. แล้วเราลบซ้ำ)
int year = raw >= 2200 ? raw - 543 : raw;
```

⚠️ **อย่าใช้ `ThaiBuddhistCalendar` / `CultureInfo("th-TH")` ในการ parse** — ผลลัพธ์ขึ้นกับ OS locale
ให้ parse ด้วย `CultureInfo.InvariantCulture` แล้วลบ 543 เองตรง ๆ จะคุมได้แน่นอน

ส่งออกทั้ง 2 ค่า: `transactionDate` (ISO) + `transactionDateBe` (ต้นฉบับ)

### 5.7 ตัวเลข

- มี comma คั่นหลักพัน: `"1,000,000"` → `1000000`
- ราคาว่างใช้ `"-"` → **ต้องเป็น `null` ห้ามเป็น `0`** (0 จะทำให้ค่าเฉลี่ยเพี้ยน)
- `&nbsp;` (U+00A0) ปนอยู่เยอะ ต้อง normalize ก่อน

```csharp
static decimal? ParseNum(string? s)
{
    if (string.IsNullOrWhiteSpace(s)) return null;
    s = s.Replace(" ", " ").Replace(",", "").Trim();
    if (s is "" or "-" or "n/a" or "N/A") return null;
    return decimal.TryParse(s, NumberStyles.Any, CultureInfo.InvariantCulture, out var v) ? v : null;
}
```

### 5.8 ชื่อบริษัท / ticker

```
"กัลฟ์ เอ็นเนอร์จี ดีเวลลอปเมนท์ ... จำกัด (มหาชน) บมจ.(GULF)"
→ symbol = GULF        (regex: \(([A-Z0-9][A-Z0-9&.\-]{0,14})\)\s*$ — ASCII เท่านั้น)
→ name   = "... จำกัด (มหาชน)"   (ตัด "บมจ.(GULF)" ออก)
```
⚠️ ต้อง anchor ท้ายสตริง + บังคับ ASCII ตัวใหญ่ ไม่งั้น `(มหาชน)` จะถูกอ่านเป็น ticker

### 5.9 แยกคำนำหน้าชื่อ

คอลัมน์ผู้บริหารเขียน `"นาย สารัชถ์ รัตนาวะดี"` (มีเว้นวรรค) แต่ในวงเล็บเขียน `"นางนลินี รัตนาวะดี"` (ไม่เว้น)
→ เวลาเทียบว่าเป็นคนเดียวกันต้อง **ตัดคำนำหน้า + ลบช่องว่างทั้งหมด** ก่อนเทียบ

คำนำหน้าที่ต้องรองรับ (เรียงยาว→สั้น เพื่อให้ `นางสาว` ชนะ `นาง`):
```
นางสาว, น.ส., นาง, นาย, ดร., ศ.ดร., รศ.ดร., ผศ.ดร., ศ., รศ., ผศ.,
พล.อ., พล.ต., พล.ท., พล.ร.อ., พล.อ.อ., พล.ต.อ., พล.ต.ท., พล.ต.ต.,
พ.อ., พ.ท., พ.ต., ร.อ., ร.ท., ร.ต., ม.ร.ว., ม.ล., คุณหญิง, ท่านผู้หญิง,
Mr., Mrs., Ms., Miss, Dr.
```

---

## 6. Parsing spec — Sustainability

หน้า `/public/idisc/th/CompanyProfile/Listed/{SYMBOL}`

| Table id | ข้อมูล | รูปแบบ |
|---|---|---|
| `gPP11T01` | CG Score | **รูปภาพเท่านั้น** |
| `gPP12T01` | AGM Level | **รูปภาพเท่านั้น** |
| `gPP17T01` | Thai-CAC | ข้อความ |
| `gPP16T01` | SET ESG Ratings | ข้อความ |
| `gPP14T01` | ข้อมูลเบื้องต้น | ที่อยู่/โทร/โทรสาร/URL |
| `gPP15T01` | ผู้ติดต่อ | IR / เลขานุการบริษัท |

อื่น ๆ: ชื่อบริษัท = `.compTitle h3` · หมวด = ลิงก์ `Ranking/Listed/Sector/([\w\-]+)` · footnote = `span.ux-remark` (บอกปีของคะแนน) · อัปเดตล่าสุด = element id ลงท้าย `lbLastupdate`

### 6.1 ⚠️ CG Score / AGM Level อยู่ในชื่อไฟล์รูป

หน้าเว็บ**ไม่มีตัวเลขเป็นข้อความเลย** ตัวเลขอยู่ในชื่อไฟล์:

```html
<img src="https://market.sec.or.th/Documents/images/cg5.gif" />   → CG  = 5
<img src="https://market.sec.or.th/Documents/images/agm4.gif" />  → AGM = 4
```

```csharp
// regex: (?:^|/)(cg|agm)([1-5])\.gif   (IgnoreCase)
```

เก็บ URL ต้นฉบับไว้ใน `rawImage` ด้วย เพื่อให้ตรวจย้อนได้

### 6.2 ⚠️ ไม่มีคะแนน ≠ คะแนนแย่

ถ้า `n/a` ต้องส่ง `isRated: false` + `score: null`
**ห้ามแปลงเป็น 0** — DELTA ได้ CG 5/5 + CAC certified แต่ไม่ได้เข้าร่วม SET ESG ถ้านับเป็น 0 จะดูแย่กว่าความจริง

ส่ง `disclosedIndicators` (0–4) บอกว่ามีข้อมูลกี่ตัวจาก 4 ด้วย

### 6.3 ⚠️ Thai-CAC มี 3 สถานะ ไม่ใช่ 2

| ข้อความบนเว็บ | status | isCertified |
|---|---|---|
| `ได้รับการรับรอง` | `certified` | **true** |
| `ประกาศเจตนารมณ์` | `declared_intent` | **false** ← ยังไม่ได้รับรอง |
| `n/a` / ว่าง | `not_certified_or_no_data` | false |

EA เป็น `declared_intent` — ถ้าเช็คแค่ "มีค่าหรือไม่" จะนับเป็น certified ผิด

---

## 7. ตาราง mapping (copy ไปใช้ได้เลย)

### 7.1 วิธีการได้มา/จำหน่าย

| ไทย | code | direction | isMarketTrade |
|---|---|---|---|
| ซื้อ | `buy` | acquire | **true** |
| ขาย | `sell` | dispose | **true** |
| ขายชอร์ต | `short_sell` | dispose | true |
| ซื้อคืน | `buy_back` | acquire | true |
| รับโอน | `transfer_in` | acquire | false |
| โอน | `transfer_out` | dispose | false |
| รับให้ | `gift_in` | acquire | false |
| ให้ | `gift_out` | dispose | false |
| รับมรดก | `inheritance_in` | acquire | false |
| ใช้สิทธิ | `exercise` | acquire | false |
| แปลงสภาพ | `conversion` | acquire | false |
| แลกเปลี่ยน | `exchange` | neutral | false |

⚠️ **`โอน` เป็น substring ของ `รับโอน`** → ต้อง match แบบ exact ก่อน ถ้าไม่เจอค่อย match substring **โดยเรียงจากยาวไปสั้น** ไม่งั้นการรับโอนจะถูกนับเป็นการโอนออก

ไม่รู้จัก → `other` / direction `unknown` → **ห้ามเดาทิศทาง** ให้ตัดออกจากการบวกลบ

### 7.2 ประเภทหลักทรัพย์

| ไทย | code | assetClass |
|---|---|---|
| หุ้นสามัญ | `common_share` | equity |
| หุ้นบุริมสิทธิ | `preferred_share` | equity |
| ใบสำคัญแสดงสิทธิที่จะซื้อหุ้น / ใบสำคัญแสดงสิทธิ | `warrant` | equity_linked |
| ใบสำคัญแสดงสิทธิอนุพันธ์ | `derivative_warrant` | derivative |
| ใบสำคัญแสดงสิทธิที่จะซื้อหุ้นเพิ่มทุนที่โอนสิทธิได้ | `tsr` | equity_linked |
| หุ้นกู้แปลงสภาพ | `convertible_debenture` | equity_linked |
| หุ้นกู้ | `debenture` | debt |
| สัญญาซื้อขายล่วงหน้า | `derivatives_contract` | derivative |
| ใบแสดงสิทธิในผลประโยชน์ | `nvdr` | equity_linked |
| หน่วยลงทุน | `unit_trust` | fund |

⚠️ เรียงยาว→สั้น: `ใบสำคัญแสดงสิทธิอนุพันธ์` ต้องชนะ `ใบสำคัญแสดงสิทธิ` และ `หุ้นกู้แปลงสภาพ` ต้องชนะ `หุ้นกู้`

### 7.3 ความสัมพันธ์ — **ลำดับนี้ห้ามสลับ**

| ลำดับ | ข้อความที่ค้น | code | isSelf | holderType |
|---|---|---|---|---|
| **1** | **นิติบุคคล** | `juristic_person` | false | juristic_person |
| 2 | ผู้รายงาน | `self` | true | individual |
| 3 | ผู้จัดทำรายงาน | `self` | true | individual |
| 4 | **ผู้จัดทำ** | `self` | true | individual |
| 5 | บุตรที่ยังไม่บรรลุนิติภาวะ | `minor_child` | false | individual |
| 6 | คู่สมรส | `spouse` | false | individual |
| 7 | ผู้สมรส | `spouse` | false | individual |
| 8 | ผู้ที่อยู่กินด้วยกันฉันสามีภริยา | `spouse` | false | individual |
| 9 | บุตร | `child` | false | individual |

> 💡 **`ผู้จัดทำ` คือคำที่เจอบ่อยที่สุด** — ในการสุ่มทดสอบ 14 บริษัท คิดเป็น **337 จาก 1,349 แถว (25%)**
> ตอนแรกเราลืม map ตัวนี้ ทำให้ 1 ใน 4 ของข้อมูลกลายเป็น `other` / `unknown` **อย่าพลาดข้อนี้**

### 7.4 CG Score / AGM Level / SET ESG

| score | CG label | AGM label |
|---|---|---|
| 5 | ดีเลิศ | ดีเยี่ยม |
| 4 | ดีมาก | ดีมาก |
| 3 | ดี | ดี |
| 2 | ดีพอใช้ | พอใช้ |
| 1 | ผ่าน | ต้องปรับปรุง |

SET ESG: `AAA`=rank 1 · `AA`=2 · `A`=3 · `BBB`=4 (rank 1 = ดีที่สุด) · เจอค่าแปลก ๆ ให้เก็บไว้ rank=null อย่าทิ้ง

### 7.5 หมวดอุตสาหกรรม (จาก Ranking link)

`SET-ENERG` → market `SET`, sector `ENERG`

```
AGRI ธุรกิจการเกษตร · FOOD อาหารและเครื่องดื่ม · FASHION แฟชั่น · HOME ของใช้ในครัวเรือนฯ
PERSON ของใช้ส่วนตัวฯ · BANK ธนาคาร · FIN เงินทุนและหลักทรัพย์ · INSUR ประกันภัยฯ
AUTO ยานยนต์ · IMM วัสดุอุตสาหกรรมฯ · PAPER กระดาษฯ · PETRO ปิโตรเคมีฯ · PKG บรรจุภัณฑ์
STEEL เหล็กฯ · CONMAT วัสดุก่อสร้าง · CONS บริการรับเหมาก่อสร้าง · PROP พัฒนาอสังหาฯ
ENERG พลังงานและสาธารณูปโภค · MINE เหมืองแร่ · COMM พาณิชย์ · HELTH การแพทย์
MEDIA สื่อและสิ่งพิมพ์ · PROF บริการเฉพาะกิจ · TOURISM การท่องเที่ยวฯ · TRANS ขนส่งฯ
ETRON ชิ้นส่วนอิเล็กทรอนิกส์ · ICT เทคโนโลยีสารสนเทศฯ · CONSUMP สินค้าอุปโภคบริโภค
SERVICE บริการ · TECH เทคโนโลยี · INDUS สินค้าอุตสาหกรรม · RESOURC ทรัพยากร
FINCIAL ธุรกิจการเงิน · PROPCON อสังหาริมทรัพย์และก่อสร้าง · AGRO เกษตรและอุตสาหกรรมอาหาร
```

---

## 8. Analytics — กฎการรวมยอด

### 8.1 ตัดออกจากยอดรวม 2 ประเภท

```
recordsUsedInTotals = ทั้งหมด − isRevoked − isPotentialDuplicate
```

### 8.2 ⚠️ แยกซื้อขายในตลาด ออกจากการโอน

**ห้ามรวม `marketActivity` กับ `nonMarketActivity`**

การโอนหุ้นในครอบครัว 550,000 หุ้น ≠ ผู้บริหารซื้อหุ้น ถ้ารวมกันตัวเลข insider buying จะผิด
แยกด้วย `isMarketTrade` (§7.1)

ใช้ชื่อ field เป็น `acquire*` / `dispose*` ไม่ใช่ `buy*` / `sell*` เพราะโครงสร้างเดียวกันใช้สรุปฝั่งการโอนด้วย

### 8.3 ⚠️ รายการซ้ำ — ก.ล.ต. เตือนเอง

หมายเหตุใต้ตารางของเว็บ ก.ล.ต.:

> กรณีที่บริษัทมีผู้บริหารเป็นคู่สมรสกัน ถ้ามีการซื้อขายหลักทรัพย์ คู่สมรสทั้ง 2 คน จะมีหน้าที่ต้องรายงาน
> ซึ่งจะทำให้เกิดการแสดงรายการซ้ำซ้อนกัน ... **จึงขอให้ใช้ข้อมูลด้วยความระมัดระวัง**

นาย A ซื้อ 1 ครั้ง → แสดง 2 แถว: `(ผู้บริหาร=A, ผู้รายงาน)` และ `(ผู้บริหาร=B, คู่สมรสของ A)`

**อัลกอริทึม dedupe:**
```
key = (holderName ที่ normalize แล้ว, date, shares, price, methodCode, securityTypeCode)
ถือว่าซ้ำ "ต่อเมื่อ" กลุ่มนั้นมี executiveName ต่างกัน ≥ 2 คน
  → เก็บแถวแรกเป็นตัวจริง ที่เหลือ flag isPotentialDuplicate
ข้ามแถว isRevoked ตอนจัดกลุ่ม (แถวที่ยกเลิกต้องไม่บังแถวจริง)
```
⚠️ ถ้า executive คนเดียวกันรายงาน 2 แถวเหมือนกัน **อย่า dedupe** — อาจเป็นการซื้อ 2 ครั้งจริงที่บังเอิญเหมือนกัน

### 8.4 ⚠️ ข้อจำกัดที่ยัง detect ไม่ได้ (ต้องบอกผู้ใช้)

บางกรณีการ**ปรับโครงสร้างผู้ถือหุ้นในกลุ่ม**ถูกยื่นเป็น `ซื้อ`/`ขาย` พร้อมราคา ทำให้ `marketActivity` พองผิดปกติ

ตัวอย่างจริง **EA ส.ค. 2565** — โอนหุ้นระหว่างบริษัทในกลุ่มที่ราคา 82.82 เท่ากันหมด:
```
2022-08-05  สมโภชน์ อาหุนัย          ขาย  936,230,000 @ 82.82
2022-08-05  บริษัท เอสพีบีแอล โฮลดิง  ซื้อ  936,230,000 @ 82.82   ← รายการเดียวกัน คนละฝั่ง
```
วัดแล้ว: **93% ของ volume ของ EA** มาจากคู่รายการแบบนี้ (9 คู่ จาก 89 market records)

เว็บหน้า list บอกไม่ได้ว่าทำผ่านตลาดหรือไม่ (ข้อมูลนั้นอยู่ในหน้า detail ที่ render ด้วย JS)
→ **ยังไม่ได้ implement** ถ้าจะทำเพิ่ม: จับคู่ (วันที่ + จำนวน + ราคา เท่ากัน + ทิศทางตรงข้าม) แล้วตั้ง flag

### 8.5 สิ่งที่ควรมีใน analytics

```
totalRecords, recordsUsedInTotals, duplicateRecordsExcluded, revokedRecordsExcluded
dateRange { first, last }
marketActivity / nonMarketActivity { records, acquireRecords, disposeRecords,
                                     acquireShares, disposeShares,
                                     acquireValue, disposeValue,
                                     netShares, netValue, netDirection }
netPositionChangeShares
byMethod[] · bySecurityType[] · byHolder[] · byExecutive[] · byMonth[] · largestTransactions[]
```

`netDirection` ∈ `net_acquisition` | `net_disposal` | `balanced` | `no_activity`

⚠️ **Compliance:** ตั้งชื่อ field เป็นข้อเท็จจริงเชิงสถิติ ห้ามสื่อเป็นคำแนะนำลงทุน และแนบ disclaimer ทุก response:

> การวิเคราะห์นี้เป็นเพียงการรวบรวมข้อมูลเพื่อการศึกษาเท่านั้น ไม่ใช่คำชี้ชวนในการลงทุน ผู้ลงทุนควรศึกษาข้อมูลเพิ่มเติมก่อนตัดสินใจ

---

## 9. Test Vectors — ใช้ตรวจว่า port ถูก

ทุกค่าด้านล่าง **ดึงจาก production จริง** ถ้า C# ได้ไม่ตรง แปลว่ายังมี bug

### 9.1 Symbol resolution

| Symbol | uniqueIDReference | ชื่อบริษัท | market-sector |
|---|---|---|---|
| GULF | `0000008616` | บริษัท กัลฟ์ ดีเวลลอปเมนท์ จำกัด (มหาชน) | SET-ENERG |
| PTT | `0000001074` | บริษัท ปตท. จำกัด (มหาชน) | SET-ENERG |
| SCB | `0000033250` | บริษัท เอสซีบี เอกซ์ จำกัด (มหาชน) | SET-BANK |
| EA | `0000006768` | บริษัท พลังงานบริสุทธิ์ จำกัด (มหาชน) | SET-ENERG |
| TWPC | `0000024002` | — | SET |
| POWER | **null** | บริษัท เพาเวอร์ โซลูชั่น เทคโนโลยี จำกัด (มหาชน) | mai-RESOURC |

Directory ทั้งหมด: **866 บริษัท** (SET 637 / mai 229), มี uniqueID **863**

### 9.2 Form 59 — GULF `0000008616`, `20220101`–`20260804`, DateType=1

```
reportedCount = 29, parsedCount = 29, parseComplete = true
duplicateRecordsExcluded = 0, revokedRecordsExcluded = 0
dateRange = 2022-01-14 .. 2025-02-21

marketActivity:     records 22 | acquire 49,625,000 หุ้น / 2,425,876,711.00 บาท
                               | dispose     750,000 หุ้น /    39,312,500.00 บาท
                               | netShares +48,875,000 → net_acquisition
nonMarketActivity:  records  7 | acquire  1,050,000 | dispose 3,550,000 | netShares −2,500,000

byHolder (ต้องแยก 2 รายนี้ออกจากกันให้ได้):
  สารัชถ์ รัตนาวะดี                        individual       net +31,100,100
  บริษัท กัลฟ์ โฮลดิ้งส์ (ประเทศไทย) จำกัด  juristic_person  net +18,324,900
byExecutive:
  สารัชถ์ รัตนาวะดี   20 records   net +49,625,000
```

แถวแรกสุด (index 0) ต้องได้:
```
executiveName = โชติกุล สุขภิรมย์เกษม   relationshipCode = self   holderIsExecutive = true
securityTypeCode = common_share          transactionDate = 2023-02-27 (จาก 27/02/2566)
shares = 550,000   pricePerShare = null   transactionValue = null
methodCode = transfer_out   direction = dispose   isMarketTrade = false   sharesSigned = −550,000
```

### 9.3 Form 59 — EA `0000006768`

| ช่วง | ผลลัพธ์ |
|---|---|
| `20240804`–`20260804` | 17 records · market 15 (ขาย 6,116,000 / 8,126,500 บาท) · non-market 2 (โอนออก 9,000,000 / 36,000,000 บาท) · ทั้งหมดเป็นของ สมบูรณ์ อาหุนัย |
| `20150101`–`20261231` | **233 records** parseComplete=true ← ยืนยันว่าไม่มี row cap |

### 9.4 Revoked — TWPC `0000024002`, `20150101`–`20261231`

ต้องได้ **6 แถว** ที่ `isRevoked = true` เช่น:
```
2025-01-15  shares 429,000  price 2.32  methodCode buy  revocationNote "Revoked by Reporter"
            → sharesSigned = null, ไม่ถูกนับใน analytics
```

### 9.5 Sustainability

| Symbol | CG | AGM | Thai-CAC | SET ESG | disclosed |
|---|---|---|---|---|---|
| GULF | 5 ดีเลิศ | 5 ดีเยี่ยม | certified | AA (rank 2) | 4/4 |
| PTT | 5 ดีเลิศ | 5 ดีเยี่ยม | certified | AAA (rank 1) | 4/4 |
| SCB | 5 ดีเลิศ | 5 ดีเยี่ยม | certified | AAA (rank 1) | 4/4 |
| EA | 3 ดี | 5 ดีเยี่ยม | **declared_intent** | ไม่ได้จัดอันดับ | 2/4 |
| DELTA | 5 ดีเลิศ | 5 ดีเยี่ยม | certified | ไม่ได้จัดอันดับ | 3/4 |
| GJS | **1 ผ่าน** | 4 ดีมาก | n/a | ไม่ได้จัดอันดับ | 2/4 |

GULF เพิ่มเติม: `rawImage` = `.../images/cg5.gif` · website `www.gulf.co.th` · โทร `0-2080-4499` ·
เลขานุการบริษัท `นางสาวฉัตรตะวัน ไชยะกุล` · IR = **null** (เว็บแสดง `-` ต้องแปลงเป็น null)

### 9.6 ทดสอบแบบสุ่ม (แนะนำให้ทำหลัง port เสร็จ)

`tests/random_sample_check.py` สุ่มบริษัทจาก 866 ตัว ดึงประวัติเต็ม แล้วจับแถวที่ code ตกไปเป็น `other`/`unknown`
— เป็นวิธีเดียวที่เจอคำศัพท์ที่ยังไม่ได้ map (fixture จับไม่ได้)

รันครั้งแรก 14 บริษัท / 1,349 แถว → เจอ **355 anomalies** (คือ bug 2 ตัวใน §5.5, §7.3)
หลังแก้ สุ่มใหม่ 16 บริษัท / 1,685 แถว → **0 anomalies**

**แนะนำให้ port script นี้เป็น integration test ของ C# ด้วย**

---

## 10. หมายเหตุสำหรับ C# โดยเฉพาะ

| เรื่อง | แนะนำ |
|---|---|
| HTML parser | **AngleSharp** (รองรับ CSS selector เช่น `span[style*='line-through']` ตรงกับที่สเปคนี้ใช้) หรือ HtmlAgilityPack + XPath |
| HttpClient | `IHttpClientFactory` + named client, `SocketsHttpHandler { PooledConnectionLifetime = 5min, MaxConnectionsPerServer = 4 }`, `AutomaticDecompression = GZip\|Deflate` |
| Encoding | `Encoding.UTF8.GetString(bytes)` เสมอ (§3.3) |
| Retry | Polly — แต่ **อย่า retry เร็ว** ถ้าเจอ bot challenge ให้ backoff 5s × 2^n |
| Cache | `IMemoryCache` + `SemaphoreSlim` ต่อ key กัน thundering herd (หลาย request ยิง scrape พร้อมกัน) |
| Rate limit | singleton (§3.4) — ถ้า deploy หลาย instance ต้องย้ายไป **Redis** ไม่งั้น rate รวมจะทะลุ |
| JSON | `JsonNamingPolicy.CamelCase`, `DefaultIgnoreCondition = Never` (ต้องส่ง `null` ออกไป ไม่ใช่ซ่อน) |
| Decimal | ใช้ `decimal` ไม่ใช่ `double` สำหรับราคา/มูลค่า |
| วันที่ | `DateOnly` + `CultureInfo.InvariantCulture` **อย่าใช้ ThaiBuddhistCalendar** (§5.6) |
| Thread safety | ตาราง mapping เป็น `static readonly` + `FrozenDictionary` (.NET 8+) |

### Scaling

Rate limiter + cache เป็น in-process → **1 instance เท่านั้น** ถ้าจะ scale หลาย pod ต้องย้าย state ไป Redis ก่อน
ไม่งั้นแต่ละ pod จะมี limiter ของตัวเอง → rate รวมทะลุ → โดนบล็อก

---

## 11. ถ้าเลือก "เรียก API แทนการ port"

ถ้ายังไม่พร้อม port เต็ม สามารถให้ efin MCP เรียก service Python ที่ deploy อยู่แล้วได้ทันที:

```
GET  /api/v1/form59/{symbol}?date_from=20240804&date_to=20260804
GET  /api/v1/sustainability/{symbol}
GET  /api/v1/symbols            (866 บริษัท)
GET  /api/v1/company/{symbol}/full
POST /api/v1/form59/bulk        body: {"symbols":[...], "date_from":"...", "date_to":"..."}
POST /api/v1/sustainability/bulk
```

และมี **MCP server พร้อมใช้** (Streamable HTTP + fixed token) ที่ `/mcp/` — 5 tools:
`lookup_thai_stock`, `search_thai_stocks`, `get_form59_executive_trades`,
`get_sustainability_ratings`, `compare_sustainability_ratings`

> ข้อจำกัด: deploy อยู่บน Railway, 1 instance, cache หายเมื่อ redeploy
> ถ้าจะใช้ production ระยะยาวแนะนำ port เป็น C# ตามเอกสารนี้

---

## 12. Checklist ส่งมอบ

- [ ] §3.2 ส่ง `DateType=1` ทุกครั้ง
- [ ] §3.3 บังคับ UTF-8
- [ ] §3.4 rate limiter global (1s / 2 concurrent) + ตรวจ `bobcmn` + ตอบ 503 ไม่ใช่ 404
- [ ] §3.6 เทียบ `parseComplete` กับจำนวนที่เว็บบอก
- [ ] §5.2 แยก `executiveName` กับ `holderName` + ส่งทั้ง `byHolder` และ `byExecutive`
- [ ] §5.3 วงเล็บซ้อนด้วยการนับ depth
- [ ] §5.4 + §7.3 `นิติบุคคล` match เป็นอันดับแรก
- [ ] §7.3 map `ผู้จัดทำ` (25% ของข้อมูล!)
- [ ] §5.5 ตรวจ `line-through` → `isRevoked` + ตัดจาก analytics
- [ ] §5.6 พ.ศ. → ค.ศ. (ไม่ใช้ ThaiBuddhistCalendar)
- [ ] §5.7 ราคาว่าง = `null` ไม่ใช่ `0`
- [ ] §6.1 อ่าน CG/AGM จากชื่อไฟล์รูป
- [ ] §6.2 `isRated=false` ≠ score 0
- [ ] §6.3 Thai-CAC 3 สถานะ
- [ ] §8.2 แยก market / non-market
- [ ] §8.3 dedupe คู่สมรสที่เป็นผู้บริหารทั้งคู่
- [ ] §8.5 แนบ disclaimer ทุก response
- [ ] §9 ผ่าน test vectors ทุกข้อ
- [ ] §9.6 รัน random sample ได้ 0 anomalies

---

## 13. ติดต่อ / อ้างอิง

| | |
|---|---|
| GitLab | https://gitlab.onlineasset.co.th/it-developer/idisc-data |
| Reference API | https://idisc-data-production.up.railway.app/docs |
| Python reference | `app/mappings.py` (vocab) · `app/services/form59.py` (parser+analytics) · `app/services/sustainability.py` · `app/http_client.py` (rate limit + challenge) |
| Tests | `tests/test_parsers.py` (99 tests) · `tests/random_sample_check.py` |
| แหล่งข้อมูล | สำนักงาน ก.ล.ต. — `market.sec.or.th` |

**ข้อควรระวังสุดท้าย:** เว็บ ก.ล.ต. ไม่มี API สัญญาไว้ โครงสร้าง HTML เปลี่ยนได้ทุกเมื่อ
`parseComplete` + random sample check คือ 2 ตัวที่จะบอกว่าเว็บเปลี่ยน — ควรตั้ง alert ไว้
