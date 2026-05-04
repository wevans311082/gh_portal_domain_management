# ResellerClub / LogicBoxes HTTP API Reference

> **Authoritative in-project reference.** Created to prevent integration mistakes.  
> Official docs: `https://manage.resellerclub.com/kb/answer/744`  
> Last updated from official KB: 2025-05

---

## Table of Contents

1. [Environments & Base URLs](#1-environments--base-urls)
2. [Authentication](#2-authentication)
3. [IP Whitelist](#3-ip-whitelist)
4. [Request Rules](#4-request-rules)
5. [Parameter Data Types](#5-parameter-data-types)
6. [Response Format](#6-response-format)
7. [Domain Endpoints](#7-domain-endpoints)
   - [Check Availability](#71-check-availability)
   - [Register](#72-register)
   - [Transfer](#73-transfer)
   - [Renew](#74-renew)
   - [Get Order Details (by Order ID)](#75-get-order-details-by-order-id)
   - [Get Order Details (by Domain Name)](#76-get-order-details-by-domain-name)
   - [Search Orders](#77-search-orders)
   - [Get Order ID](#78-get-order-id)
   - [Modify Name Servers](#79-modify-name-servers)
   - [Modify Contacts](#710-modify-contacts)
   - [Enable Theft Protection Lock](#711-enable-theft-protection-lock)
   - [Disable Theft Protection Lock](#712-disable-theft-protection-lock)
   - [Get Locks Applied](#713-get-locks-applied)
   - [Get Auth Code (EPP)](#714-get-auth-code-epp)
   - [Modify Auth Code](#715-modify-auth-code)
   - [Delete Domain](#716-delete-domain)
   - [Restore Domain](#717-restore-domain)
   - [Cancel Transfer](#718-cancel-transfer)
   - [DNSSEC: Add DS Record](#719-dnssec-add-ds-record)
   - [DNSSEC: Delete DS Record](#720-dnssec-delete-ds-record)
   - [Privacy Protection](#721-privacy-protection)
8. [Contacts Endpoints](#8-contacts-endpoints)
   - [Add Contact](#81-add-contact)
   - [Modify Contact](#82-modify-contact)
   - [Get Contact Details](#83-get-contact-details)
   - [Search Contacts](#84-search-contacts)
   - [Delete Contact](#85-delete-contact)
9. [Customer Endpoints](#9-customer-endpoints)
10. [Pricing Endpoints](#10-pricing-endpoints)
    - [Get Customer Pricing](#101-get-customer-pricing)
    - [Get Reseller Pricing](#102-get-reseller-pricing)
11. [DNS Management Endpoints](#11-dns-management-endpoints)
12. [Product Keys](#12-product-keys)
13. [TLD Notes & Exceptions](#13-tld-notes--exceptions)
14. [Common Response Hash Map Fields](#14-common-response-hash-map-fields)
15. [CRITICAL GOTCHAS — Read Before Writing Any API Code](#15-critical-gotchas--read-before-writing-any-api-code)
16. [Our Implementation Reference](#16-our-implementation-reference)

---

## 1. Environments & Base URLs

| Environment | Base URL | Purpose |
|---|---|---|
| **Live / Production** | `https://httpapi.com/api` | All production API calls |
| **Test / Sandbox** | `https://test.httpapi.com/api` | Development and testing |
| **Domain Check (alternative)** | `https://domaincheck.httpapi.com/api` | Domain availability checks only |

> **Note:** The test environment allows GET requests even for mutating operations. The live environment strictly requires POST for mutating calls.

All endpoints use the `.json` suffix (or `.xml` for XML responses). This project uses `.json` throughout.

**Example live URL:**
```
https://httpapi.com/api/domains/available.json?auth-userid=12345&api-key=mykey&domain-name=example&tlds=com
```

---

## 2. Authentication

Every request—without exception—must include these two parameters:

| Parameter | Type | Description |
|---|---|---|
| `auth-userid` | Integer | Your Reseller account ID (not customer ID) |
| `api-key` | String | Your API key from the Reseller control panel |

**Placement:**
- GET requests: as query string parameters
- POST requests: as form-encoded body parameters (same request body as other params)

**⛔ HTTP Basic Auth is NOT supported.** Using it causes JWT/token errors.

**How to find your credentials:**
- Login to [manage.resellerclub.com](https://manage.resellerclub.com)
- Go to: Settings → API → Generate / View API Key
- Your `auth-userid` is shown as your Reseller ID

---

## 3. IP Whitelist

- ResellerClub requires your server's IP to be whitelisted before API calls will succeed.
- You can whitelist up to **3 IP addresses**.
- Whitelisting takes **30–60 minutes** to activate after adding.
- Configure at: Settings → API → IP Whitelist in the Reseller control panel.
- On the **test environment**, IP whitelisting is NOT required.

---

## 4. Request Rules

| Rule | Detail |
|---|---|
| GET for reads | `domains/available`, `domains/details`, `contacts/details`, etc. |
| POST for mutations | `domains/register`, `domains/renew`, `contacts/add`, etc. |
| All values URL-encoded | Standard percent-encoding |
| Parameter values are **case-sensitive** | `KeepInvoice` ≠ `keepinvoice` |
| Array parameters | Repeat the same parameter name: `ns=ns1.example.com&ns=ns2.example.com` |
| Map parameters | Use numeric suffixes: `attr-name1=key&attr-value1=val&attr-name2=key2&attr-value2=val2` |
| Response format | JSON (use `.json` suffix) or XML (use `.xml` suffix) |

---

## 5. Parameter Data Types

| Type | Description | Example |
|---|---|---|
| `Integer` | Whole number | `1`, `0`, `-1` |
| `Float` | Decimal number | `0.0`, `12.99` |
| `Boolean` | true/false | `true`, `false` |
| `String` | Text value, case-sensitive | `"KeepInvoice"` |
| `Array of Strings` | Repeat param name with each value | `tlds=com&tlds=net&tlds=org` |
| `Map[name]` / `Map[value]` | Key-value pairs using numeric suffix | `attr-name1=idnLanguageCode&attr-value1=de` |

---

## 6. Response Format

All JSON responses return one of:

**Success:** A hash map (JSON object) with the requested data.

**Error:**
```json
{"status": "ERROR", "message": "Description of the error"}
```

**Action Response (for mutations):** Contains:
```json
{
  "description": "domain.com",
  "entityid": 12345678,
  "actiontype": "Register",
  "actiontypedesc": "Registration of domain.com",
  "eaqid": 98765,
  "actionstatus": "Success",
  "actionstatusdesc": "Domain has been registered successfully",
  "invoiceid": 111222,
  "sellingcurrencysymbol": "GBP",
  "sellingamount": "12.99",
  "unutilisedsellingamount": "0.0",
  "customerid": 456789,
  "discount-amount": "0.0"
}
```

> `invoiceid`, `sellingcurrencysymbol`, `sellingamount`, `unutilisedsellingamount`, and `customerid` are **NOT returned** if `invoice-option` is set to `NoInvoice`.

---

## 7. Domain Endpoints

### 7.1 Check Availability

**Endpoint:** `GET domains/available.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `domain-name` | Array of Strings | Yes | Domain label(s) only — **NOT** the full domain. For `example.com`, pass `example`. |
| `tlds` | Array of Strings | Yes | TLD(s) to check — e.g., `com`, `net`, `co.uk` |

> **Important:** `domain-name` must be the label **without** the TLD. Passing `example.com` is wrong. Pass `example`.

**Example:**
```
GET https://httpapi.com/api/domains/available.json?auth-userid=0&api-key=key&domain-name=example&domain-name=mysite&tlds=com&tlds=net
```

**Response:** A hash map keyed by `{label}.{tld}` with:
- `status`:
  - `available` — can be registered
  - `regthroughus` — currently registered through your registrar connection
  - `regthroughothers` — registered through another registrar (transfer possible)
  - `unknown` — registry unreachable; try again later
- `tm-claims-key` — present only if `available` and in Trademark Clearinghouse
- `costHash` — present only for premium domains; includes `create`, `renew`, `transfer` pricing and currency symbol

**Limits:**
- Donuts TLDs: maximum **5 domain names per Donuts TLD group** per call; more returns `unknown`
- `.CA`: maximum **15** strings per call

---

### 7.2 Register

**Endpoint:** `POST domains/register.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `domain-name` | String | Yes | Full domain name: `example.com` |
| `years` | Integer | Yes | Registration period. Note: `.AI` can only be registered for 2 years. |
| `ns` | Array of Strings | Yes | Name servers (e.g., `ns1.example.com`, `ns2.example.com`) |
| `customer-id` | Integer | Yes | Customer account ID |
| `reg-contact-id` | Integer | Yes | Registrant contact ID |
| `admin-contact-id` | Integer | Yes | Admin contact ID. Pass `-1` for: `.EU`, `.RU`, `.UK` |
| `tech-contact-id` | Integer | Yes | Technical contact ID. Pass `-1` for: `.EU`, `.FR`, `.RU`, `.UK` |
| `billing-contact-id` | Integer | Yes | Billing contact ID. Pass `-1` for: `.BERLIN`, `.CA`, `.EU`, `.FR`, `.NL`, `.NZ`, `.RU`, `.UK`, `.LONDON` |
| `invoice-option` | String | Yes | `NoInvoice`, `PayInvoice`, `KeepInvoice`, `OnlyAdd` |
| `auto-renew` | Boolean | Yes | Enable/disable auto-renewal |
| `purchase-privacy` | Boolean | Optional | Adds privacy protection. **Not supported** for: `.ASIA`, `.AU`, `.CA`, `.CL`, `.CN`, `.DE`, `.ES`, `.EU`, `.FR`, `.IN`, `.NL`, `.NZ`, `.PRO`, `.RU`, `.SX`, `.TEL`, `.UK`, `.US` |
| `protect-privacy` | Boolean | Optional | Enables/disables privacy protection setting |
| `discount-amount` | Float | Optional | Discount amount |
| `purchase-premium-dns` | Boolean | Optional | Add Premium DNS service |
| `attr-name1` / `attr-value1` | Map | Optional | Extra details (see below) |

**`attr-name`/`attr-value` pairs (common uses):**

| Use Case | Key | Value |
|---|---|---|
| IDN language code | `idnLanguageCode` | e.g., `de`, `fr`, `zh`, `ko` |
| `.AU` eligibility type | `id-type` | `ACN`, `ABN`, etc. |
| `.CN` hosting clause | `cnhosting` | `true`; also `cnhostingclause=yes` |
| `.TEL` whois type | `whois-type` | `Natural` or `Legal` |
| Premium domain | `premium` | `true` |
| EAP domain | `eap` | `true` |
| Sunrise phase | `phase` | `sunrise`; also `smd=<smd_file_content>` |
| `.ASIA` CED contact | `cedcontactid` | Contact ID |

**Invoice Option values:**

| Value | Behaviour |
|---|---|
| `NoInvoice` | No invoice raised; order executes immediately |
| `PayInvoice` | Invoice raised; paid if customer has funds; else order pends |
| `KeepInvoice` | Invoice raised for later payment; order executes |
| `OnlyAdd` | Invoice raised for later payment; registration action remains pending |

**Response:** Standard action hash map (see section 6). Includes `privacydetails` if `purchase-privacy=true`.

---

### 7.3 Transfer

**Endpoint:** `POST domains/transfer.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `domain-name` | String | Yes | Full domain to transfer |
| `auth-code` | String | Optional* | EPP authorization code. Required for: `.AU`, `.BIZ`, `.BZ`, `.CA`, `.CO`, `.COM`, `.DE`, `.EU`, `.IN`, `.INFO`, `.MN`, `.MOBI`, `.NAME`, `.NET`, `.NL`, `.NZ`, `.ORG`, `.US`, `.WS`, `.XXX` |
| `customer-id` | Integer | Yes | Customer account ID |
| `reg-contact-id` | Integer | Yes | Registrant contact |
| `admin-contact-id` | Integer | Yes | Admin contact. Pass `-1` for `.EU`, `.RU`, `.UK` |
| `tech-contact-id` | Integer | Yes | Tech contact. Pass `-1` for `.EU`, `.RU`, `.UK` |
| `billing-contact-id` | Integer | Yes | Billing contact. Pass `-1` for `.AT`, `.BERLIN`, `.CA`, `.NL`, `.NZ`, `.RU`, `.UK` |
| `invoice-option` | String | Yes | Same options as Register |
| `auto-renew` | Boolean | Yes | Auto-renewal setting |
| `ns` | Array of Strings | Optional | Name servers (max 13) |
| `purchase-privacy` | Boolean | Optional | Same restrictions as Register |
| `purchase-premium-dns` | Boolean | Optional | Add Premium DNS |
| `attr-name1` / `attr-value1` | Map | Optional | Extra details (see below) |

**Transfer-specific `attr-name`/`attr-value` pairs:**

| Use Case | Key | Value |
|---|---|---|
| `.ASIA` CED contact | `cedcontactid` | Contact ID |
| Premium domain | `type` | `premium` |
| Aftermarket premium | `type` | `premiumft`; also `premiumprice={price}` |
| `.SCOT` / `.NZ` EEA contact | `tnc` | `Y` |

**Response:** Standard action hash map. On pending/waiting-for-registry, `actionstatus` returns `NoError` rather than `ERROR`.

---

### 7.4 Renew

**Endpoint:** `POST domains/renew.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `order-id` | Integer | Yes | Order ID of the domain registration order |
| `years` | Integer | Yes | Renewal period. Note: `.AI` can only be renewed for 2 years. |
| `exp-date` | Integer | Yes | Current expiry date as **Unix epoch timestamp** |
| `auto-renew` | Boolean | Yes | Auto-renewal setting |
| `invoice-option` | String | Yes | Same options as Register |
| `purchase-privacy` | Boolean | Optional | Renew privacy protection (same TLD restrictions apply) |
| `discount-amount` | Float | Optional | Discount amount |
| `purchase-premium-dns` | Boolean | Optional | Add Premium DNS |
| `attr-name1` / `attr-value1` | Map | Optional | For premium domains: `attr-name1=premium&attr-value1=true` |

**Example:**
```
POST https://httpapi.com/api/domains/renew.json
Body: auth-userid=0&api-key=key&order-id=562994&years=1&exp-date=1279012036&invoice-option=NoInvoice
```

---

### 7.5 Get Order Details (by Order ID)

**Endpoint:** `GET domains/details.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `order-id` | Integer | Yes | Order ID |
| `options` | Array of Strings | Yes | What data to return. See table below. |

**`options` values:**

| Value | Returns |
|---|---|
| `All` | Everything |
| `OrderDetails` | Core order fields: domain name, status, dates, product key, NS |
| `ContactIds` | Contact IDs only |
| `RegistrantContactDetails` | Registrant contact data |
| `AdminContactDetails` | Admin contact data |
| `TechContactDetails` | Tech contact data |
| `BillingContactDetails` | Billing contact data |
| `NsDetails` | Name server details |
| `DomainStatus` | `domainstatus`, RAA verification status |
| `DNSSECDetails` | DS records |
| `StatusDetails` | `currentstatus`, suspension flags |

**Key response fields:**

| Field | Description |
|---|---|
| `orderid` | Order ID |
| `domainname` | Full domain name |
| `currentstatus` | `InActive`, `Active`, `Suspended`, `Pending Delete Restorable`, `Deleted`, `Archived` |
| `orderstatus` | Registry locks: `resellersuspend`, `resellerlock`, `transferlock` |
| `domainstatus` | System holds: `sixtydaylock`, `renewhold` |
| `productcategory` | Product category (e.g., `domainnames`) |
| `productkey` | Product key (e.g., `dotcom`) |
| `creationtime` | Registration date (epoch) |
| `endtime` | Expiry date (epoch) |
| `ns1`, `ns2` | Nameservers |
| `domsecret` | Domain secret / auth code |
| `isprivacyprotected` | Boolean |
| `recurring` | Auto-renewal enabled |
| `registrantcontactid` | Registrant contact ID |
| `dnssec` | DNSSEC DS records array |
| `gdpr.enabled` | GDPR protection status |

---

### 7.6 Get Order Details (by Domain Name)

**Endpoint:** `GET domains/details-by-name.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `domain-name` | String | Yes | Full domain name |
| `options` | Array of Strings | Yes | Same as 7.5 |

---

### 7.7 Search Orders

**Endpoint:** `GET domains/search.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `no-of-records` | Integer | Yes | Max records to return |
| `page-no` | Integer | Yes | Page number (1-based) |
| `order-by` | Array of Strings | Optional | Sort fields |
| `domain-name` | String | Optional | Filter by domain name |
| `customer-id` | Integer | Optional | Filter by customer |
| `status` | String | Optional | `Active`, `InActive`, etc. |
| `expiry-date` | Integer | Optional | Filter by expiry (epoch) |

---

### 7.8 Get Order ID

**Endpoint:** `GET domains/orderid.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `domain-name` | String | Yes | Full domain name |

**Response:** Integer order ID.

---

### 7.9 Modify Name Servers

**Endpoint:** `POST domains/modify-ns.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `order-id` | Integer | Yes | Order ID |
| `ns` | Array of Strings | Yes | New name servers |
| `otp` | Integer | Optional | OTP for 2FA authentication |
| `2fa-type` | String | Optional | `email` for email-based OTP |

---

### 7.10 Modify Contacts

**Endpoint:** `POST domains/modify-contacts.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `order-id` | Integer | Yes | Order ID |
| `reg-contact-id` | Integer | Yes | New registrant contact ID |
| `admin-contact-id` | Integer | Yes | New admin contact ID |
| `tech-contact-id` | Integer | Yes | New tech contact ID |
| `billing-contact-id` | Integer | Yes | New billing contact ID |
| `sixty-day-lock-optout` | Boolean | Optional | Opt-out of 60-day lock on contact change |

---

### 7.11 Enable Theft Protection Lock

**Endpoint:** `POST domains/enable-theft-protection.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `order-id` | Integer | Yes | Order ID |
| `otp` | Integer | Optional | OTP for 2FA |
| `2fa-type` | String | Optional | `email` |

---

### 7.12 Disable Theft Protection Lock

**Endpoint:** `POST domains/disable-theft-protection.json`

**Parameters:** Same as Enable (7.11).

---

### 7.13 Get Locks Applied

**Endpoint:** `GET domains/locks.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `order-id` | Integer | Yes | Order ID |

---

### 7.14 Get Auth Code (EPP)

**Endpoint:** `GET domains/auth-code.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `order-id` | Integer | Yes | Order ID |

**Response:** Contains the domain secret / EPP auth code.

---

### 7.15 Modify Auth Code

**Endpoint:** `POST domains/modify-auth-code.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `order-id` | Integer | Yes | Order ID |
| `auth-code` | String | Yes | New auth code |

---

### 7.16 Delete Domain

**Endpoint:** `POST domains/delete.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `order-id` | Integer | Yes | Order ID |

---

### 7.17 Restore Domain

**Endpoint:** `POST domains/restore.json`  
Used to restore a domain in `Pending Delete Restorable` status.

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `order-id` | Integer | Yes | Order ID |
| `invoice-option` | String | Yes | `NoInvoice`, `PayInvoice`, `KeepInvoice`, `OnlyAdd` |

---

### 7.18 Cancel Transfer

**Endpoint:** `POST domains/cancel-transfer.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `order-id` | Integer | Yes | Order ID |

---

### 7.19 DNSSEC: Add DS Record

**Endpoint:** `POST domains/add-ds-record.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `order-id` | Integer | Yes | Order ID |
| `attr-name1` | String | Yes | `keytag` |
| `attr-value1` | String | Yes | Key tag value |
| `attr-name2` | String | Yes | `algorithm` |
| `attr-value2` | String | Yes | Algorithm number |
| `attr-name3` | String | Yes | `digesttype` |
| `attr-value3` | String | Yes | Digest type |
| `attr-name4` | String | Yes | `digest` |
| `attr-value4` | String | Yes | Digest hex string |

---

### 7.20 DNSSEC: Delete DS Record

**Endpoint:** `POST domains/delete-ds-record.json`

**Parameters:** Same as Add DS Record.

---

### 7.21 Privacy Protection

**Purchase / Renew:**  
`POST domains/purchase-privacy.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `order-id` | Integer | Yes | Order ID |
| `invoice-option` | String | Yes | Invoice option |
| `purchase-privacy` | Boolean | Yes | `true` |

**Enable/Disable Privacy Protection:**  
`POST domains/modify-privacy-protection.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `order-id` | Integer | Yes | Order ID |
| `protect-privacy` | Boolean | Yes | `true` to enable, `false` to disable |

**TLDs where Privacy Protection is NOT supported:**
`.ASIA`, `.AU`, `.CA`, `.CL`, `.CN`, `.DE`, `.ES`, `.EU`, `.FR`, `.IN`, `.NL`, `.NZ`, `.PRO`, `.RU`, `.SX`, `.TEL`, `.UK`, `.US` and ccTLD subdomains `.ORG.CO`, `.MIL.CO`, `.GOV.CO`, `.EDU.CO`

---

## 8. Contacts Endpoints

### 8.1 Add Contact

**Endpoint:** `POST contacts/add.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `name` | String | Yes | Contact name (max 255 chars; max 50 for `.EU`) |
| `company` | String | Yes | Company name (max 255 chars). Use `NA` if no company for EuContact; `N A` or `Not Applicable` for RuContact. |
| `email` | String | Yes | Email address |
| `address-line-1` | String | Yes | Street address (max 64 chars) |
| `city` | String | Yes | City (max 64 chars) |
| `country` | String | Yes | ISO 3166-1 alpha-2 country code |
| `zipcode` | String | Yes | Postal code (max 10 chars; 16 for `.EU`) |
| `phone-cc` | String | Yes | Phone country code (1–3 digits) |
| `phone` | String | Yes | Phone number (4–12 digits; 4–13 for `.RU`) |
| `customer-id` | Integer | Yes | Customer account ID |
| `type` | String | Yes | Contact type (see below) |
| `address-line-2` | String | Optional | Second address line |
| `address-line-3` | String | Optional | Third address line |
| `state` | String | Optional | State/province (max 64 chars; specific values for `.ES`) |
| `fax-cc` | String | Optional | Fax country code |
| `fax` | String | Optional | Fax number |
| `attr-name1` / `attr-value1` | Map | Optional | Extra TLD-specific details |

**Contact `type` values:**

| Value | Use Case |
|---|---|
| `Contact` | Generic / default contact type |
| `BrContact` | Brazil `.BR` domains |
| `BrOrgContact` | Brazil organization contacts |
| `CaContact` | Canada `.CA` domains |
| `ClContact` | Chile `.CL` domains |
| `CnContact` | China `.CN` domains |
| `CoContact` | Colombia `.CO` domains |
| `DeContact` | Germany `.DE` domains |
| `EsContact` | Spain `.ES` domains |
| `EuContact` | EU `.EU` domains |
| `FrContact` | France `.FR` domains |
| `MxContact` | Mexico (recommended for `.LAT`) |
| `NlContact` | Netherlands `.NL` domains |
| `NycContact` | `.NYC` domains (must be NYC address) |
| `RuContact` | Russia `.RU` domains |
| `UkContact` | UK `.UK` domains |
| `UkServiceContact` | UK service contacts (GB, IM, JE, GG only) |

**Response:** Integer Contact ID on success.

---

### 8.2 Modify Contact

**Endpoint:** `POST contacts/modify.json`

**Parameters:** Same as Add Contact, plus:

| Name | Type | Required | Description |
|---|---|---|---|
| `contact-id` | Integer | Yes | ID of contact to modify |

---

### 8.3 Get Contact Details

**Endpoint:** `GET contacts/details.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `contact-id` | Integer | Yes | Contact ID |

---

### 8.4 Search Contacts

**Endpoint:** `GET contacts/search.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `customer-id` | Integer | Yes | Customer ID |
| `no-of-records` | Integer | Yes | Max records |
| `page-no` | Integer | Yes | Page number |
| `contact-id` | Integer | Optional | Filter by ID |
| `name` | String | Optional | Filter by name |
| `company` | String | Optional | Filter by company |
| `email` | String | Optional | Filter by email |
| `type` | Array | Optional | Filter by contact type |
| `status` | Array | Optional | Filter by status |

---

### 8.5 Delete Contact

**Endpoint:** `POST contacts/delete.json`

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |
| `contact-id` | Integer | Yes | Contact ID to delete |

---

## 9. Customer Endpoints

| Operation | Method | Endpoint |
|---|---|---|
| Sign Up | POST | `customers/signup.json` |
| Modify Details | POST | `customers/modify.json` |
| Get Details by Username | GET | `customers/details.json?username=email@example.com` |
| Get Details by ID | GET | `customers/details-by-id.json?customer-id=123` |
| Generate Token | GET | `customers/generate-token.json` |
| Authenticate Token | GET | `customers/authenticate-token.json` |
| Change Password | POST | `customers/change-password.json` |
| Search | GET | `customers/search.json` |
| Delete | POST | `customers/delete.json` |

---

## 10. Pricing Endpoints

### 10.1 Get Customer Pricing  *(VERIFIED LIVE 2026-05)*

**Endpoint:** `GET products/customer-price.json`  
KB: `https://manage.resellerclub.com/kb/answer/3449`

**⚠️ Critical:** Despite what older KB articles imply, this endpoint takes **only auth params** —
any `productkey` / `action` / `years` filters are **ignored**.  A single call returns the
**ENTIRE customer pricing catalog** for every product on your reseller account
(≈120KB, 400+ classkey entries).  Calling it once per TLD per action (the obvious-looking pattern)
will cause 504 timeouts under any kind of bulk sync.

**Parameters:**

| Name | Type | Required | Description |
|---|---|---|---|
| `auth-userid` | Integer | Yes | Auth |
| `api-key` | String | Yes | Auth |

**Example:**
```
GET https://httpapi.com/api/products/customer-price.json?auth-userid=614860&api-key=...
```

**Response structure (real shape):**
```json
{
  "domcno": {
    "addnewdomain":      { "1": 13.59, "2": 13.59, "...": "...", "10": 13.59 },
    "renewdomain":       { "1": 13.59, "...": "...", "10": 13.59 },
    "addtransferdomain": { "1": 13.59 },
    "restoredomain":     { "1": 53.99 }
  },
  "dotnet":          { "addnewdomain": { "1": 14.79 } },
  "thirdleveldotuk": { "addnewdomain": { "1": 8.39  } }
}
```

Key points:
* The top-level key is a **classkey**, NOT a TLD string and NOT `<tld>-domain`.
  Examples (verified live): `.com→domcno`, `.net→dotnet`, `.org→domorg`, `.io→dotio`,
  `.uk→dotuk`, `.co.uk`/`.org.uk→thirdleveldotuk`, `.de→dotde`, `.us→domus`,
  `.info→dominfo`, `.biz→dombiz`, `.com.au→thirdleveldotau`.
* Action keys are the catalog names: `addnewdomain` (registration),
  `renewdomain` (renewal), `addtransferdomain` (transfer), `restoredomain` (restore).
* Year keys are **strings** (`"1"`..`"10"`).
* Prices are decimal numbers in your reseller's selling currency (no /100 conversion).
* TLDs without an `addtransferdomain` block do not support transfers via the API
  (e.g. some 3rd-level UK and `.es`).

#### How to discover the classkey for a TLD

There is no direct "TLD → classkey" endpoint. Use **`domains/available.json`** — every
availability response embeds the `classkey` per TLD:

```
GET domains/available.json?domain-name=probe&tlds=com&tlds=net&tlds=co.uk
→ { "probe.com":   { "classkey": "domcno",          "status": "..." },
    "probe.net":   { "classkey": "dotnet",          "status": "..." },
    "probe.co.uk": { "classkey": "thirdleveldotuk", "status": "..." } }
```

A full pricing sync for N TLDs is therefore exactly **2 API calls**:
1. One chunked `domains/available.json` sweep → map every TLD → classkey.
2. One `products/customer-price.json` → full catalog.

This is implemented in `ResellerClubClient.prime_pricing_cache()` and used by `TLDPricingService.sync_pricing()`.

> **This is the endpoint our `TLDPricingService` uses.** Do not call it per-TLD.

---

### 10.2 Get Reseller Pricing

**Endpoint:** `GET products/reseller-price.json`  
KB: `https://manage.resellerclub.com/kb/answer/3451`

Returns what you (as reseller) pay to ResellerClub. Same parameters as 10.1.

---

## 11. DNS Management Endpoints

ResellerClub provides DNS management for domains using their nameservers. DNS operations use the `dns/manage/` path prefix.

**Activate DNS Service first:** `POST dns/activate.json` (requires `order-id`, `invoice-option`)

### DNS Record Operations

All DNS endpoints require `auth-userid` and `api-key`.

| Operation | Method | Endpoint | Key Params |
|---|---|---|---|
| Add A (IPv4) record | POST | `dns/manage/add-ipv4-record.json` | `order-id`, `host`, `value` (IP), `ttl` |
| Add AAAA (IPv6) record | POST | `dns/manage/add-ipv6-record.json` | `order-id`, `host`, `value` (IPv6), `ttl` |
| Add CNAME record | POST | `dns/manage/add-cname-record.json` | `order-id`, `host`, `value` (target), `ttl` |
| Add MX record | POST | `dns/manage/add-mx-record.json` | `order-id`, `host`, `value` (mail server), `ttl` |
| Add NS record | POST | `dns/manage/add-ns-record.json` | `order-id`, `host`, `value` (NS), `ttl` |
| Add TXT record | POST | `dns/manage/add-txt-record.json` | `order-id`, `host`, `value`, `ttl` |
| Add SRV record | POST | `dns/manage/add-srv-record.json` | `order-id`, `host`, `value`, `ttl` |
| Modify A record | POST | `dns/manage/update-ipv4-record.json` | `order-id`, `host`, `current-value`, `new-value`, `ttl` |
| Modify AAAA record | POST | `dns/manage/update-ipv6-record.json` | Same pattern |
| Modify CNAME record | POST | `dns/manage/update-cname-record.json` | Same pattern |
| Modify MX record | POST | `dns/manage/update-mx-record.json` | Same pattern |
| Modify TXT record | POST | `dns/manage/update-txt-record.json` | Same pattern |
| Modify SRV record | POST | `dns/manage/update-srv-record.json` | Same pattern |
| Modify SOA record | POST | `dns/manage/update-soa-record.json` | `order-id`, `responsible-person`, `refresh`, `retry`, `expire`, `ttl` |
| Search DNS records | GET | `dns/manage/search-records.json` | `order-id`, `type`, `no-of-records`, `page-no` |
| Delete A record | POST | `dns/manage/delete-ipv4-record.json` | `order-id`, `host`, `value` |
| Delete AAAA record | POST | `dns/manage/delete-ipv6-record.json` | Same pattern |
| Delete CNAME record | POST | `dns/manage/delete-cname-record.json` | Same pattern |
| Delete MX record | POST | `dns/manage/delete-mx-record.json` | Same pattern |
| Delete NS record | POST | `dns/manage/delete-ns-record.json` | Same pattern |
| Delete TXT record | POST | `dns/manage/delete-txt-record.json` | Same pattern |
| Delete SRV record | POST | `dns/manage/delete-srv-record.json` | Same pattern |

> **Note:** The generic `dns/manage/delete-record.json` is **deprecated**. Use the type-specific delete endpoints above.

---

## 12. Product Keys

Product keys are used in pricing API calls. The `productkey` parameter maps to TLDs.

**Common product key format:** `dot{tld}` (lowercased, dots removed)

| TLD | Product Key |
|---|---|
| `.com` | `dotcom` |
| `.net` | `dotnet` |
| `.org` | `dotorg` |
| `.info` | `dotinfo` |
| `.biz` | `dotbiz` |
| `.co.uk` | `dotco_uk` |
| `.org.uk` | `dotorg_uk` |
| `.me.uk` | `dotme_uk` |
| `.de` | `dotde` |
| `.in` | `dotin` |
| `.co.in` | `dotco_in` |
| `.us` | `dotus` |
| `.ca` | `dotca` |
| `.com.au` | `dotcom_au` |
| `.net.au` | `dotnet_au` |
| `.eu` | `doteu` |
| `.io` | `dotio` |
| `.me` | `dotme` |
| `.tv` | `dottv` |
| `.co` | `dotco` |
| `.club` | `dotclub` |
| `.online` | `dotonline` |

> For the complete canonical list for your reseller account, check the admin panel at: Settings → Products → Domain Names, or call `products/plan-details.json`.

**KB reference:** `https://manage.resellerclub.com/kb/answer/1918`

---

## 13. TLD Notes & Exceptions

### Contact ID Rules

| TLD | `admin-contact-id` | `tech-contact-id` | `billing-contact-id` |
|---|---|---|---|
| `.EU` | Pass `-1` | Pass `-1` | Pass `-1` |
| `.RU` | Pass `-1` | Pass `-1` | Pass `-1` |
| `.UK` | Pass `-1` | Pass `-1` | Pass `-1` |
| `.FR` | Normal | Pass `-1` | Pass `-1` |
| `.BERLIN`, `.CA`, `.NL`, `.NZ`, `.LONDON` | Normal | Normal | Pass `-1` |

### Registration Period Restrictions

| TLD | Restriction |
|---|---|
| `.AI` | Can only be registered **or** renewed for **2 years** |

### Auth Code Required for Transfer

`.AU`, `.BIZ`, `.BZ`, `.CA`, `.CO`, `.COM`, `.DE`, `.EU`, `.IN`, `.INFO`, `.MN`, `.MOBI`, `.NAME`, `.NET`, `.NL`, `.NZ`, `.ORG`, `.US`, `.WS`, `.XXX`

### No TLD Discovery Endpoint

**⚠️ There is NO API endpoint for listing available TLDs.**  
ResellerClub does not expose any `tlds/list`, `domains/tlds`, or similar endpoint.  
Any attempt to call such endpoints will return `404 Not Found`.  
The correct approach is a maintained curated list (see `SUPPORTED_TLDS` in `apps/domains/resellerclub_client.py`).

---

## 14. Common Response Hash Map Fields

For domain action responses (register, transfer, renew, modify, etc.):

| Field | Key | Description |
|---|---|---|
| Domain name | `description` | Full domain name |
| Order ID | `entityid` | Numeric order ID |
| Action type | `actiontype` | e.g., `Register`, `Renew`, `Transfer` |
| Action description | `actiontypedesc` | Human-readable action description |
| Action ID | `eaqid` | Numeric action/event ID |
| Action status | `actionstatus` | `Success`, `InProgress`, `Failed` |
| Action status description | `actionstatusdesc` | Human-readable status |
| Invoice ID | `invoiceid` | Only present if invoice was created |
| Currency | `sellingcurrencysymbol` | e.g., `GBP`, `USD` |
| Amount | `sellingamount` | Transaction amount |
| Unutilised amount | `unutilisedsellingamount` | Unused portion |
| Customer ID | `customerid` | Customer account ID |
| Discount | `discount-amount` | Discount applied |
| Privacy details | `privacydetails` | Present only if privacy was purchased |

---

## 15. CRITICAL GOTCHAS — Read Before Writing Any API Code

### ❌ Myth: There is a TLD listing endpoint
**Reality:** No such endpoint exists. Calls to `domains/tlds.json`, `products/list.json`, `domains/available-tlds.json` will all 404.  
**Fix:** Use a curated `SUPPORTED_TLDS` list. See `apps/domains/resellerclub_client.py`.

### ❌ Myth: HTTP Basic Auth works
**Reality:** The API does not support HTTP Basic Auth. Attempting to use it causes JWT/token errors.  
**Fix:** Pass `auth-userid` and `api-key` as query params (GET) or form body params (POST) on every request.

### ❌ Myth: `domain-name` in availability check is the full domain
**Reality:** For `domains/available.json`, the `domain-name` parameter is the **label only** (without TLD). Pass `example`, not `example.com`.  
**Fix:** Strip everything from the first `.` onward before passing to the availability endpoint.

### ❌ Myth: Dates are ISO strings
**Reality:** The API uses Unix epoch timestamps (integers) for all dates. `exp-date` in renew must be an epoch integer.  
**Fix:** Use `int(datetime.timestamp())`.

### ❌ Myth: Parameter names are case-insensitive
**Reality:** Parameter values are **case-sensitive**. `NoInvoice` ≠ `noinvoice`. `true` ≠ `True`.

### ❌ Myth: The test URL is fine for production
**Reality:** The test URL `test.httpapi.com` is a sandbox. It does not process real orders and has relaxed rules (GET allowed for mutations). Always use `httpapi.com` in production.  
**This project default:** `https://httpapi.com/api` (configurable via `RESELLERCLUB_API_URL` env var).

### ❌ Myth: The pricing response has a simple numeric price keyed by years
**Reality:** `customer-price.json` returns the **full reseller catalog** keyed by
`classkey` → `action` → `years` → decimal price. Top-level keys are classkeys
(`domcno`, `dotnet`, `thirdleveldotuk`, ...), NOT years. Year keys are strings.
Prices are plain decimals in your reseller currency — no /100 conversion.  
**Fix:** Navigate `catalog[classkey]["addnewdomain"][str(years)]`.

### ❌ Myth: `customer-price.json` filters by `productkey` / `action` / `years`
**Reality:** Those parameters are silently ignored — you always get the entire
catalog (≈120KB, 400+ entries). Calling per-TLD per-action causes 504 timeouts.  
**Fix:** Call ONCE, cache the response, look up classkeys via
`domains/available.json` (which returns `classkey` for every TLD).

### ❌ Myth: Any IP can call the live API
**Reality:** The live API requires your server's IP to be whitelisted. Takes 30–60 minutes after adding.  
**Fix:** Whitelist IPs in the Reseller control panel before going live.

### ✅ Always use `.json` suffix
Endpoints must end with `.json` (or `.xml`). The client code appends this automatically if missing.

### ✅ Retry on 5xx
The API can return 500/502/503/504 transiently. Use retry logic (this project uses `urllib3.Retry` with 3 retries and 0.5s backoff).

### ✅ Array params must be repeated
Do NOT use comma-separated values for arrays. Use repeated parameters:  
`tlds=com&tlds=net&tlds=org` — correct  
`tlds=com,net,org` — **wrong**: API treats the whole comma string as a single literal TLD and
responds with `{"<label>.<comma-string>": {"status": "unknown"}}` — the canonical signature of this bug.

### ✅ Discover TLD classkeys via `domains/available.json`
`classkey` is included in every availability response and is the join key into the
customer pricing catalog. One chunked availability call gives you the classkey for
every TLD you support — do this once and cache the mapping.

---

## 16. Our Implementation Reference

### Configuration Settings

| Setting | Env Var | Default | Description |
|---|---|---|---|
| API Base URL | `RESELLERCLUB_API_URL` | `https://httpapi.com/api` | Live URL. Change to `https://test.httpapi.com/api` for sandbox. |
| Reseller ID | `RESELLERCLUB_RESELLER_ID` | _(required)_ | Your reseller account ID |
| API Key | `RESELLERCLUB_API_KEY` | _(required)_ | Your API key |
| Debug Mode | `RESELLERCLUB_DEBUG_MODE` | `false` | Set to `true` to log full request/response data |

These can be set as:
1. Environment variables (`.env` file / Docker env)
2. Runtime settings via Django admin: Admin Tools → Integration Settings

### Client Class: `ResellerClubClient`

File: `apps/domains/resellerclub_client.py`

| Method | Endpoint | Description |
|---|---|---|
| `check_availability(domain_names, tlds)` | `GET domains/available` | Check domain availability. Sends `domain-name` and `tlds` as **repeated** params; chunks TLDs to keep URLs under safe length. Response includes `classkey` per TLD. |
| `discover_tld_classkeys(tlds, probe_label="example")` | `GET domains/available` | Returns `{tld: classkey}` mapping built from a single availability sweep. Caches results on the client instance. |
| `get_customer_pricing()` | `GET products/customer-price` | Fetches the **full** customer pricing catalog (one call). Cached for the lifetime of the client instance. |
| `prime_pricing_cache(tlds)` | `GET domains/available` + `GET products/customer-price` | One-shot: discover classkeys for the given TLDs and load the full pricing catalog. Total: 2 API calls. |
| `get_tld_pricing(tld, years, action)` | _(cache lookup)_ | Returns pricing block for a single TLD/action from the cached catalog. Triggers cache priming on miss. |
| `get_tld_costs(tld, years)` | _(cache lookup)_ | Registration + renewal + transfer costs from the cached catalog. |
| `suggest_names(keyword, tlds)` | `GET domains/suggest-names` | Get name suggestions |
| `list_available_tlds()` | _(no API call)_ | Returns curated `SUPPORTED_TLDS` list |
| `register_domain(...)` | `POST domains/register` | Register a domain |
| `renew_domain(order_id, years, exp_date)` | `POST domains/renew` | Renew a domain |
| `get_order_details(order_id)` | `GET domains/details` | Get full order details |
| `modify_nameservers(order_id, ns_list)` | `POST domains/modify-ns` | Change nameservers |
| `lock_domain(order_id)` | `POST domains/enable-theft-protection` | Lock domain |
| `unlock_domain(order_id)` | `POST domains/disable-theft-protection` | Unlock domain |
| `get_auth_code(order_id)` | `GET domains/auth-code` | Get EPP/transfer code |
| `add_dns_record(order_id, host, value, type, ttl)` | `POST dns/manage/add-record` | Add DNS record |
| `delete_dns_record(order_id, host, value, type)` | `POST dns/manage/delete-record` | Delete DNS record |
| `create_contact(payload)` | `POST contacts/add` | Create a contact |
| `update_contact(contact_id, payload)` | `POST contacts/modify` | Update a contact |
| `get_contact(contact_id)` | `GET contacts/details` | Get contact details |

### TLD Pricing Sync

File: `apps/domains/pricing.py`, class: `TLDPricingService`

- Calls `list_available_tlds()` → gets curated list (no API call)
- For each TLD: calls `get_tld_costs(tld)` → 3 API calls (registration/renewal/transfer)
- Failed TLDs are logged to `last_sync_error`, not aborted
- Pricing stored in `TLDPricing` model

### Admin Manual Sync Actions

File: `apps/admin_tools/views.py`

| Action | URL | Behaviour |
|---|---|---|
| `import_all_tlds` | `/admin-tools/import-tlds/` | Calls `list_available_tlds()` then runs `TLDPricingService().sync_pricing()` inline |
| `sync_all` | `/admin-tools/sync-all/` | Runs `TLDPricingService().sync_pricing()` inline |
| `sync_tld` | `/admin-tools/sync-tld/<tld>/` | Syncs a single TLD |

All admin actions execute synchronously (no Celery dependency).

---

## Official Documentation Links

| Topic | URL |
|---|---|
| HTTP API Overview | https://manage.resellerclub.com/kb/answer/744 |
| Access and Authentication | https://manage.resellerclub.com/kb/answer/753 |
| Request Parameter Data Types | https://manage.resellerclub.com/kb/answer/755 |
| Response Formats | https://manage.resellerclub.com/kb/answer/754 |
| Product Keys | https://manage.resellerclub.com/kb/answer/1918 |
| Domains — All | https://manage.resellerclub.com/kb/answer/750 |
| Check Availability | https://manage.resellerclub.com/kb/answer/764 |
| Register | https://manage.resellerclub.com/kb/answer/752 |
| Transfer | https://manage.resellerclub.com/kb/answer/758 |
| Renew | https://manage.resellerclub.com/kb/answer/746 |
| Search | https://manage.resellerclub.com/kb/answer/771 |
| Get Order Details (Order ID) | https://manage.resellerclub.com/kb/answer/770 |
| Get Order Details (Domain Name) | https://manage.resellerclub.com/kb/answer/1755 |
| Modify Name Servers | https://manage.resellerclub.com/kb/answer/776 |
| Modify Contacts | https://manage.resellerclub.com/kb/answer/777 |
| Enable Lock | https://manage.resellerclub.com/kb/answer/902 |
| Disable Lock | https://manage.resellerclub.com/kb/answer/903 |
| Get Auth Code | https://manage.resellerclub.com/kb/answer/779 |
| Delete Domain | https://manage.resellerclub.com/kb/answer/745 |
| Restore Domain | https://manage.resellerclub.com/kb/answer/760 |
| Privacy Protection | https://manage.resellerclub.com/kb/answer/2085 |
| Add DNSSEC DS Record | https://manage.resellerclub.com/kb/answer/1910 |
| Contacts — All | https://manage.resellerclub.com/kb/answer/789 |
| Add Contact | https://manage.resellerclub.com/kb/answer/790 |
| Modify Contact | https://manage.resellerclub.com/kb/answer/791 |
| Get Contact | https://manage.resellerclub.com/kb/answer/792 |
| Search Contacts | https://manage.resellerclub.com/kb/answer/793 |
| Customers — All | https://manage.resellerclub.com/kb/answer/803 |
| Products — All | https://manage.resellerclub.com/kb/answer/831 |
| Get Customer Pricing | https://manage.resellerclub.com/kb/answer/3449 |
| Get Reseller Pricing | https://manage.resellerclub.com/kb/answer/3451 |
| DNS — All | https://manage.resellerclub.com/kb/answer/829 |
| Managing DNS Records | https://manage.resellerclub.com/kb/answer/1091 |
