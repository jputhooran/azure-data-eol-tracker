#!/usr/bin/env python3
"""
Keep the "Azure data services version & end-of-support dashboard" up to date and send reminders.

Runs as an Azure Automation runbook (type Python, runtime version 3.10). It only uses the
Python standard library, so there are no packages to import. The dashboard template imports it
and starts it once a week from a small Logic App.
Nothing in this file needs to be edited for a normal setup: the job finds the dashboard by
itself and takes its settings (including who gets the reminder emails) from the dashboard.

Each run:
  1. Reads Microsoft's published support dates: the Microsoft Learn version policy pages for
     Azure Database for PostgreSQL and MySQL flexible server, the Microsoft Lifecycle page of
     each SQL Server version, and the supported versions page of Azure Cosmos DB for PostgreSQL.
     Retirement dates of whole services (Azure Cache for Redis, single servers, MariaDB) are
     kept in the SERVICE_RETIREMENTS list below. endoflife.date can be used instead of Learn.
     If a page can't be read or the dates don't look right, the dashboard keeps its current
     dates and the job ends as Failed.
  2. Saves the dates in the dashboard with today's date and prints what changed.
  3. Finds every data service whose support ends within the dashboard's Red threshold
     (180 days by default), or has already ended, and emails one summary to the addresses in
     the dashboard's "Reminder emails to" setting, plus a personal list to each owner whose
     email address is in the owner tag. Nothing is sent when nothing needs a reminder. The
     emails go out through the Logic App deployed with the dashboard template.

The Automation account's managed identity needs:
  - Workbook Contributor on the dashboard's resource group
  - Logic App Contributor on the Logic App (for the reminders)
  - Reader on every subscription to check (or on a management group above them)
The dashboard template creates the Automation account and assigns the first two.

Optional arguments when you start the runbook by hand:
  --dry-run                  check everything and report what would change and who would be
                             emailed, but don't save or send anything
  --source=endoflife.date    read the dates from endoflife.date instead of Microsoft Learn
"""

import datetime
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

# =================================== SETTINGS ===================================
# Leave these empty: the job finds the dashboard by itself. Fill them in only if you have
# several copies of the dashboard and the job can't tell which one to use.
SUBSCRIPTION_ID = ""
RESOURCE_GROUP = ""
# Dashboard name as shown under Azure Monitor > Workbooks.
WORKBOOK_NAME = "Azure data services version & end-of-support dashboard"
# Optional: the dashboard's full resource ID. If you set it, the three values above aren't used.
WORKBOOK_RESOURCE_ID = ""
# Where to read the dates from: "microsoft-learn" (recommended) or "endoflife.date".
SOURCE = "microsoft-learn"
# Optional: client ID of a user-assigned managed identity. Leave empty to use the
# Automation account's system-assigned identity.
USER_ASSIGNED_CLIENT_ID = ""
# True = check everything and report what would change and who would be emailed,
# but don't save or send anything.
DRY_RUN = False
# Optional: dates you want to set yourself. They're reapplied after every refresh.
# Examples: {("PostgreSQL", "14"): {"standardSupportEnds": "2027-01-31"},
#            ("SQL Server", "2016"): {"extendedSupportEnds": "2029-07-17"}}
# Fields (YYYY-MM-DD): standardSupportEnds (for SQL Server: end of extended support),
# mainstreamSupportEnds, extendedSupportStarts and extendedSupportEnds (paid support:
# Extended Support for PostgreSQL and MySQL, Extended Security Updates for SQL Server).
DATE_OVERRIDES = {}

# ---- Reminder emails for Red servers ----
# True = email reminders about Red servers. The Red threshold and the owner tag name come
# from the dashboard's settings, so the emails always match what the dashboard shows.
SEND_REMINDERS = True
# Who gets the weekly summary is set in the dashboard ("Reminder emails to"). Addresses here
# are only used when that dashboard setting is empty. Separate several with semicolons.
TEAM_EMAILS = ""
# True = also email each owner whose address is in a server's owner tag (default tag "Owner").
EMAIL_OWNERS = True
# True = also include servers that are already past the end of support.
INCLUDE_PAST_SUPPORT = True
# Logic App that sends the emails, in the dashboard's resource group. The dashboard template
# records its name in the dashboard; this name is only used when the dashboard doesn't say.
LOGIC_APP_NAME = "la-db-eol-reminders"
# ================================================================================

RUNBOOK_VERSION = "4.0"
ENGINES = ("PostgreSQL", "MySQL", "SQL Server", "Cosmos DB for PostgreSQL",
           "PostgreSQL single server", "MySQL single server", "MariaDB",
           "Azure Cache for Redis", "Azure Cache for Redis Enterprise")
# Services Microsoft retires as a whole, whatever the version: (retirement date, announcement).
# If Microsoft moves a date, change it here or add it to DATE_OVERRIDES with version "all".
SERVICE_RETIREMENTS = {
    "PostgreSQL single server": ("2025-03-28", "https://learn.microsoft.com/en-us/lifecycle/end-of-support/end-of-support-2025"),
    "MySQL single server": ("2024-09-16", "https://learn.microsoft.com/en-us/azure/mysql/migrate/whats-happening-to-mysql-single-server"),
    "MariaDB": ("2025-09-19", "https://learn.microsoft.com/en-us/lifecycle/announcements/azure-products-retirement-september-2025"),
    "Azure Cache for Redis": ("2028-09-30", "https://learn.microsoft.com/en-us/azure/azure-cache-for-redis/retirement-faq"),
    "Azure Cache for Redis Enterprise": ("2027-03-31", "https://learn.microsoft.com/en-us/azure/azure-cache-for-redis/retirement-faq"),
}
ALL_VERSIONS = "all"
# Sources that may disappear (the service itself is retiring). If they can't be read, their dates are kept.
OPTIONAL_ENGINES = ("Cosmos DB for PostgreSQL",)
# standardSupportEnds is the date each server's status is based on. For SQL Server it's the
# end of extended support (the last free security update), and the paid dates are the
# Extended Security Updates (ESU) period.
DATE_FIELDS = ("standardSupportEnds", "mainstreamSupportEnds", "extendedSupportStarts", "extendedSupportEnds")
FIELD_LABELS = {
    "standardSupportEnds": "support end",
    "mainstreamSupportEnds": "mainstream support end",
    "extendedSupportStarts": "paid support start",
    "extendedSupportEnds": "paid support end",
}

LEARN_URLS = {
    "pg_policy": "https://learn.microsoft.com/en-us/azure/postgresql/configure-maintain/concepts-version-policy",
    "pg_extended": "https://learn.microsoft.com/en-us/azure/postgresql/configure-maintain/extended-support",
    "mysql_policy": "https://learn.microsoft.com/en-us/azure/mysql/concepts-version-policy",
    "cosmos_pg_versions": "https://learn.microsoft.com/en-us/azure/cosmos-db/postgresql/reference-versions",
}
# SQL Server: one Microsoft Lifecycle page per version. Versions released later are found
# automatically (the job looks for a page named sql-server-<year>).
SQL_LIFECYCLE_URL = "https://learn.microsoft.com/en-us/lifecycle/products/%s"
SQL_LIFECYCLE_PAGES = {
    "2012": "microsoft-sql-server-2012",
    "2014": "sql-server-2014",
    "2016": "sql-server-2016",
    "2017": "sql-server-2017",
    "2019": "sql-server-2019",
    "2022": "sql-server-2022",
    "2025": "sql-server-2025",
}
SQL_LIFECYCLE_SEARCH_URL = "https://learn.microsoft.com/en-us/lifecycle/products/?terms=SQL%20Server"
SQL_ESU_URL = "https://learn.microsoft.com/en-us/sql/sql-server/end-of-support/sql-server-extended-security-updates"
SQL_OLDEST_VERSION = 2012               # oldest version SQL Server enabled by Azure Arc reports
SQL_FREE_ESU_ON_AZURE_VMS = ("2014",)    # versions that get free Extended Security Updates on Azure VMs
EOL_API_URLS = {
    "PostgreSQL": "https://endoflife.date/api/v1/products/azure-database-for-postgresql/",
    "MySQL": "https://endoflife.date/api/v1/products/azure-database-for-mysql/",
    "SQL Server": "https://endoflife.date/api/v1/products/mssqlserver/",
}
EOL_PAGE_URLS = {
    "PostgreSQL": "https://endoflife.date/azure-database-for-postgresql",
    "MySQL": "https://endoflife.date/azure-database-for-mysql",
    "SQL Server": "https://endoflife.date/mssqlserver",
}
SOURCE_LABELS = {"microsoft-learn": "Microsoft Learn", "endoflife.date": "endoflife.date"}
ENGINE_TITLES = {
    "PostgreSQL": "Azure Database for PostgreSQL flexible server",
    "MySQL": "Azure Database for MySQL flexible server",
    "SQL Server": "SQL Server (enabled by Azure Arc, or on Azure VMs)",
    "Cosmos DB for PostgreSQL": "Azure Cosmos DB for PostgreSQL",
    "PostgreSQL single server": "Azure Database for PostgreSQL single server",
    "MySQL single server": "Azure Database for MySQL single server",
    "MariaDB": "Azure Database for MariaDB",
    "Azure Cache for Redis": "Azure Cache for Redis (Basic, Standard and Premium)",
    "Azure Cache for Redis Enterprise": "Azure Cache for Redis (Enterprise and Enterprise Flash)",
}
ENGINE_MICROSOFT_SOURCES = {
    "PostgreSQL": [("version policy", LEARN_URLS["pg_policy"]), ("extended support", LEARN_URLS["pg_extended"])],
    "MySQL": [("version support policy", LEARN_URLS["mysql_policy"])],
    "SQL Server": [("Microsoft Lifecycle", SQL_LIFECYCLE_SEARCH_URL), ("Extended Security Updates", SQL_ESU_URL)],
    "Cosmos DB for PostgreSQL": [("supported versions", LEARN_URLS["cosmos_pg_versions"])],
}
for _engine, (_, _url) in SERVICE_RETIREMENTS.items():
    ENGINE_MICROSOFT_SOURCES[_engine] = [("retirement announcement", _url)]

# Sanity limits used when checking what was read.
MIN_VERSIONS_WITH_DATES = {"PostgreSQL": 4, "MySQL": 2, "SQL Server": 5, "Cosmos DB for PostgreSQL": 3}
EARLIEST_DATE = datetime.date(2015, 1, 1)
LATEST_DATE = datetime.date(2045, 12, 31)

ARM_ENDPOINT = "https://management.azure.com"
WORKBOOK_API_VERSION = "2022-04-01"
RETRY_DELAY_SECONDS = 5

# Names used inside the dashboard. They must match the dashboard template.
PARAM_DATA = "Lifecycle_Data"
PARAM_EXPRESSIONS = {
    "standardSupportEnds": "Lifecycle_StandardSupportEnds",
    "mainstreamSupportEnds": "Lifecycle_MainstreamSupportEnds",
    "extendedSupportStarts": "Lifecycle_ExtendedSupportStarts",
    "extendedSupportEnds": "Lifecycle_ExtendedSupportEnds",
}
PARAM_CHECKED = "Lifecycle_LastChecked"
PARAM_CHECKED_ISO = "Lifecycle_LastCheckedIso"
PARAM_SOURCE = "Lifecycle_Source"
TEXT_ITEM_NAME = "lifecycle dates table"
# Settings the dashboard template writes and the job reads.
PARAM_TEAM_EMAILS = "TeamEmails"
PARAM_LOGIC_APP = "ReminderLogicApp"
# How the job recognises the dashboard: this tag (set by the template), or the dashboard name.
DASHBOARD_TAG = "hidden-db-eol-dashboard"
LEGACY_WORKBOOK_NAMES = ("Azure database version & end-of-support dashboard",)
REQUIRED_PARAMS = [PARAM_DATA, PARAM_CHECKED, PARAM_CHECKED_ISO, PARAM_SOURCE] + list(PARAM_EXPRESSIONS.values())

NOTE_NOT_LISTED = "Not found in the source at the last check; last known dates kept."
NOTE_OVERRIDE = "Date set manually in the update job."

MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july",
               "august", "september", "october", "november", "december"]
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Tests replace these to run without network access.
_transport = None
_today_override = None
_now_override = None


class UpdateError(Exception):
    """A problem that stops the update. Nothing is saved when this is raised."""


def log(message=""):
    print(message, flush=True)


# --------------------------------------------------------------------------- HTTP

RETRY_STATUSES = (408, 429, 500, 502, 503, 504)


def http_request(method, url, headers=None, body=None, timeout=60, attempts=3, retry_statuses=RETRY_STATUSES):
    """Send a request and return (status, text). Retries timeouts, throttling and server errors."""
    all_headers = {"User-Agent": "azure-db-eol-dashboard-updater/" + RUNBOOK_VERSION}
    all_headers.update(headers or {})
    data = body.encode("utf-8") if isinstance(body, str) else body
    status, text, network_error = None, None, None
    for attempt in range(1, attempts + 1):
        status, text, network_error = None, None, None
        try:
            if _transport is not None:
                status, text = _transport(method, url, all_headers, data)
            else:
                request = urllib.request.Request(url, data=data, headers=all_headers, method=method)
                try:
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        status = response.status
                        text = response.read().decode("utf-8", "replace")
                except urllib.error.HTTPError as err:
                    status = err.code
                    text = err.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as err:
            network_error = str(err)
        if status is not None and status not in retry_statuses:
            return status, text
        if attempt < attempts:
            time.sleep(RETRY_DELAY_SECONDS * attempt)
    if status is None:
        host = urllib.parse.urlsplit(url).netloc
        raise UpdateError("Couldn't reach %s: %s" % (host, network_error))
    return status, text


def fetch_page(url):
    status, text = http_request("GET", url, {"Accept": "text/html", "Accept-Language": "en-US"})
    if status != 200 or not text:
        raise UpdateError("Couldn't read %s (HTTP %s)." % (url, status))
    return text


def _short(text, limit=400):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


# --------------------------------------------------------------------------- dates and versions

def _month_number(token):
    token = token.lower().rstrip(".")
    if token == "sept":
        token = "sep"
    if len(token) < 3:
        return None
    for index, name in enumerate(MONTH_NAMES):
        if name.startswith(token):
            return index + 1
    return None


_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})[-\s]([A-Za-z]{3,9})\.?[-\s,]+(\d{4})\b"), "dmy"),
    (re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b"), "mdy"),
]


def parse_date(text):
    """Find a date such as 14-Nov-2030, November 14, 2030, 14 November 2030 or 2030-11-14."""
    if not text:
        return None
    for pattern, order in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            try:
                if order == "ymd":
                    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
                elif order == "dmy":
                    day, month, year = int(match.group(1)), _month_number(match.group(2)), int(match.group(3))
                else:
                    month, day, year = _month_number(match.group(1)), int(match.group(2)), int(match.group(3))
                if not month:
                    continue
                return datetime.date(year, month, day)
            except ValueError:
                continue
    return None


def iso(date_value):
    return date_value.isoformat() if date_value else None


def from_iso(text):
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(str(text).strip())
    except ValueError:
        return None


def display_date(date_value):
    if isinstance(date_value, str):
        date_value = from_iso(date_value)
    if not date_value:
        return ""
    return "%d %s %d" % (date_value.day, MONTH_ABBR[date_value.month - 1], date_value.year)


def extract_version(text, engine):
    """'PostgreSQL 18' -> '18', 'MySQL 8.0' -> '8.0', 'SQL Server 2008 R2' -> '2008 R2'."""
    text = text or ""
    if engine == "SQL Server":
        match = re.search(r"\b(20\d\d)(\s*R2)?\b", text, re.IGNORECASE)
        if not match:
            return None
        return match.group(1) + (" R2" if match.group(2) else "")
    if engine == "MySQL":
        match = re.search(r"\b(\d{1,2}\.\d{1,2})\b", text)
        return match.group(1) if match else None
    match = re.search(r"\b(\d{1,2})(?:\.(\d{1,2}))?\b", text)
    if not match:
        return None
    if int(match.group(1)) >= 10:
        return str(int(match.group(1)))
    return "%s.%s" % (match.group(1), match.group(2)) if match.group(2) else None


def _version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", str(version)))


_VERSION_FORMAT = re.compile(r"\d{1,2}(\.\d{1,2})?|\d{4}( R2)?")


def _valid_version(version):
    """'14', '8.0', '2019', '2008 R2' and 'all' are valid. Anything else could break the dashboard query."""
    return version == ALL_VERSIONS or bool(_VERSION_FORMAT.fullmatch(str(version or "")))


def _entry_key(entry):
    return (ENGINES.index(entry["engine"]) if entry["engine"] in ENGINES else 99, _version_key(entry["version"]))


def _new_entry(engine, version):
    entry = {"engine": engine, "version": version}
    for field in DATE_FIELDS:
        entry[field] = None
    entry["note"] = ""
    return entry


def _add_note(existing, note):
    existing = (existing or "").strip()
    if not note or note in existing:
        return existing
    return (existing + " " + note).strip()


# --------------------------------------------------------------------------- reading HTML tables

def _clean_cell(text):
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


class _TableParser(HTMLParser):
    """Collects the text of every top-level <table> on a page as rows of cells."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._depth = 0
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self.tables.append([])
            return
        if self._depth != 1:
            return
        if tag == "tr":
            self._finish_row()
            self._row = []
        elif tag in ("td", "th"):
            self._finish_cell()
            if self._row is None:
                self._row = []
            self._cell = []
        elif tag in ("br", "p", "li", "div") and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag == "table":
            if self._depth == 1:
                self._finish_row()
            self._depth = max(0, self._depth - 1)
            return
        if self._depth != 1:
            return
        if tag in ("td", "th"):
            self._finish_cell()
        elif tag == "tr":
            self._finish_row()

    def handle_data(self, data):
        if self._depth == 1 and self._cell is not None:
            self._cell.append(data)

    def _finish_cell(self):
        if self._cell is not None and self._row is not None:
            self._row.append(_clean_cell("".join(self._cell)))
        self._cell = None

    def _finish_row(self):
        self._finish_cell()
        if self._row and self.tables:
            self.tables[-1].append(self._row)
        self._row = None


def html_tables(page):
    parser = _TableParser()
    parser.feed(page)
    parser.close()
    return parser.tables


def _header_key(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


_COLUMN_TESTS = [
    ("version", lambda h: "version" in h and "date" not in h and "minor" not in h and "upgrade" not in h),
    ("std_end", lambda h: "standard support end" in h),
    ("ext_start", lambda h: "extended support start" in h),
    ("ext_end", lambda h: "extended support end" in h),
    # Microsoft Lifecycle pages (SQL Server)
    ("listing", lambda h: h == "listing" or h.startswith("listing ")),
    ("mainstream_end", lambda h: "mainstream end" in h),
    ("extended_end", lambda h: h.startswith("extended end")),
    ("start", lambda h: h == "start date"),
    ("end", lambda h: h == "end date"),
    # Azure Cosmos DB for PostgreSQL
    ("retire", lambda h: "retirement date" in h),
]


def find_table(tables, required):
    """Return the rows of the first table whose header has all the required columns."""
    for rows in tables:
        if len(rows) < 2:
            continue
        header = [_header_key(cell) for cell in rows[0]]
        columns = {}
        for name, test in _COLUMN_TESTS:
            for index, heading in enumerate(header):
                if test(heading):
                    columns[name] = index
                    break
        if all(name in columns for name in required):
            return [{name: (row[index] if index < len(row) else "") for name, index in columns.items()}
                    for row in rows[1:]]
    return None


# --------------------------------------------------------------------------- sources

def read_microsoft_learn():
    """Read the dates from the Microsoft Learn version pages. Returns ({(engine, version): entry}, warnings)."""
    found, warnings = {}, []

    # PostgreSQL: the version policy page has every version and its standard support end date.
    rows = find_table(html_tables(fetch_page(LEARN_URLS["pg_policy"])), ("version", "std_end"))
    if rows is None:
        raise UpdateError("Couldn't find the PostgreSQL support table on %s. The page layout may have changed."
                          % LEARN_URLS["pg_policy"])
    policy = {}
    for row in rows:
        version, end = extract_version(row["version"], "PostgreSQL"), parse_date(row["std_end"])
        if version and end:
            policy[version] = end

    # PostgreSQL: the extended support page adds the Extended Support start and end dates.
    extended = {}
    rows = find_table(html_tables(fetch_page(LEARN_URLS["pg_extended"])), ("version", "ext_end"))
    if rows is None:
        warnings.append("Couldn't find the Extended Support table on %s, so PostgreSQL Extended Support dates "
                        "were left as they were." % LEARN_URLS["pg_extended"])
    else:
        for row in rows:
            version = extract_version(row["version"], "PostgreSQL")
            if version:
                extended[version] = {
                    "std_end": parse_date(row.get("std_end", "")),
                    "ext_start": parse_date(row.get("ext_start", "")),
                    "ext_end": parse_date(row.get("ext_end", "")),
                }

    for version in sorted(set(policy) | set(extended), key=_version_key):
        entry = _new_entry("PostgreSQL", version)
        candidates = []
        if version in policy:
            candidates.append(policy[version])
        ext = extended.get(version)
        if ext:
            if ext["std_end"]:
                candidates.append(ext["std_end"])
            entry["extendedSupportStarts"] = iso(ext["ext_start"])
            entry["extendedSupportEnds"] = iso(ext["ext_end"])
        if not candidates:
            continue
        entry["standardSupportEnds"] = iso(min(candidates))
        distinct = sorted(set(candidates))
        if len(distinct) > 1:
            entry["note"] = ("Microsoft's pages give different end dates (%s); the earlier one is used."
                             % " and ".join(display_date(d) for d in distinct))
        found[("PostgreSQL", version)] = entry

    # MySQL: one table has all three dates.
    rows = find_table(html_tables(fetch_page(LEARN_URLS["mysql_policy"])), ("version", "std_end"))
    if rows is None:
        raise UpdateError("Couldn't find the MySQL support table on %s. The page layout may have changed."
                          % LEARN_URLS["mysql_policy"])
    for row in rows:
        version, end = extract_version(row["version"], "MySQL"), parse_date(row["std_end"])
        if not version or not end:
            continue
        entry = _new_entry("MySQL", version)
        entry["standardSupportEnds"] = iso(end)
        entry["extendedSupportStarts"] = iso(parse_date(row.get("ext_start", "")))
        entry["extendedSupportEnds"] = iso(parse_date(row.get("ext_end", "")))
        found[("MySQL", version)] = entry

    # SQL Server: one Microsoft Lifecycle page per version.
    sql_found, sql_warnings = read_sql_server_lifecycle()
    found.update(sql_found)
    warnings.extend(sql_warnings)

    # Azure Cosmos DB for PostgreSQL: one table of PostgreSQL versions and their retirement dates.
    cosmos_found, cosmos_warnings = read_cosmos_postgresql()
    found.update(cosmos_found)
    warnings.extend(cosmos_warnings)
    return found, warnings


def read_cosmos_postgresql():
    """PostgreSQL versions in Azure Cosmos DB for PostgreSQL. Problems are warnings: the old dates are kept."""
    url = LEARN_URLS["cosmos_pg_versions"]
    status, text = _get_page(url)
    if status != 200 or not text:
        return {}, ["Couldn't read %s (HTTP %s), so the Cosmos DB for PostgreSQL dates were left as they were."
                    % (url, status)]
    rows = find_table(html_tables(text), ("version", "retire"))
    found = {}
    for row in rows or []:
        version, end = extract_version(row["version"], "PostgreSQL"), parse_date(row["retire"])
        if version and end:
            entry = _new_entry("Cosmos DB for PostgreSQL", version)
            entry["standardSupportEnds"] = iso(end)
            found[("Cosmos DB for PostgreSQL", version)] = entry
    if not found:
        return {}, ["Couldn't find the Cosmos DB for PostgreSQL version table on %s. The page layout may have changed, "
                    "so its dates were left as they were." % url]
    return found, []


def service_retirement_entries():
    """Retirement dates of whole services, from SERVICE_RETIREMENTS."""
    found = {}
    for engine, (date_text, _) in SERVICE_RETIREMENTS.items():
        entry = _new_entry(engine, ALL_VERSIONS)
        entry["standardSupportEnds"] = iso(from_iso(date_text))
        found[(engine, ALL_VERSIONS)] = entry
    return found


def read_dates(source):
    """All the dates the dashboard needs, from the chosen source plus the service retirement list."""
    if source == "microsoft-learn":
        found, warnings = read_microsoft_learn()
    else:
        found, warnings = read_endoflife_date()
    found.update(service_retirement_entries())
    return found, warnings


def sql_server_entry(page, version):
    """Read one SQL Server Microsoft Lifecycle page. Returns None if it has no dates for the version."""
    tables = html_tables(page)
    rows = find_table(tables, ("listing", "mainstream_end", "extended_end"))
    entry = None
    for row in rows or []:
        extended_end = parse_date(row["extended_end"])
        if extract_version(row["listing"], "SQL Server") == version and extended_end:
            entry = _new_entry("SQL Server", version)
            entry["standardSupportEnds"] = iso(extended_end)
            entry["mainstreamSupportEnds"] = iso(parse_date(row["mainstream_end"]))
            break
    if entry is None:
        return None
    # The Releases table lists each year of Extended Security Updates.
    esu = []
    for row in find_table(tables, ("version", "start", "end")) or []:
        if re.search(r"extended security update", row["version"], re.IGNORECASE):
            start, end = parse_date(row["start"]), parse_date(row["end"])
            if start and end:
                esu.append((start, end))
    if esu:
        entry["extendedSupportStarts"] = iso(min(start for start, _ in esu))
        entry["extendedSupportEnds"] = iso(max(end for _, end in esu))
    return entry


def _get_page(url, attempts=3):
    """Like fetch_page, but returns (status, text) instead of stopping."""
    try:
        return http_request("GET", url, {"Accept": "text/html", "Accept-Language": "en-US"}, attempts=attempts)
    except UpdateError as error:
        return None, str(error)


def read_sql_server_lifecycle():
    """Read every SQL Server version's dates from its Microsoft Lifecycle page."""
    found, warnings = {}, []
    for version in sorted(SQL_LIFECYCLE_PAGES, key=_version_key):
        url = SQL_LIFECYCLE_URL % SQL_LIFECYCLE_PAGES[version]
        status, text = _get_page(url)
        if status != 200 or not text:
            warnings.append("Couldn't read %s (HTTP %s), so the SQL Server %s dates were left as they were."
                            % (url, status, version))
            continue
        entry = sql_server_entry(text, version)
        if entry is None:
            warnings.append("Couldn't find the SQL Server %s support dates on %s. The page layout may have changed, "
                            "so its dates were left as they were." % (version, url))
            continue
        found[("SQL Server", version)] = entry
    # A newer SQL Server version gets a page named sql-server-<year>. Look for the next few years.
    newest = max(int(version) for version in SQL_LIFECYCLE_PAGES)
    for year in range(newest + 1, _today().year + 3):
        version = str(year)
        status, text = _get_page(SQL_LIFECYCLE_URL % ("sql-server-" + version), attempts=1)
        if status == 200 and text:
            entry = sql_server_entry(text, version)
            if entry is not None:
                found[("SQL Server", version)] = entry
    return found, warnings


def _endoflife_sql_server(releases):
    """endoflife.date lists SQL Server by service pack. Use the latest dates of each version."""
    found = {}
    for release in releases:
        version = extract_version(str(release.get("label") or release.get("name") or ""), "SQL Server")
        end = parse_date(str(release.get("eolFrom") or ""))
        if not version or not end or _version_key(version)[0] < SQL_OLDEST_VERSION:
            continue
        current = found.get(("SQL Server", version))
        if current is not None and current["standardSupportEnds"] >= iso(end):
            continue
        entry = _new_entry("SQL Server", version)
        entry["standardSupportEnds"] = iso(end)
        entry["extendedSupportEnds"] = iso(parse_date(str(release.get("eoesFrom") or "")))
        found[("SQL Server", version)] = entry
    return found


def read_endoflife_date():
    """Read the dates from the endoflife.date API. It has no Extended Support start dates, no
    PostgreSQL Extended Support dates and no SQL Server mainstream or ESU start dates, so those
    are kept as they were."""
    found = {}
    for engine in ENGINES:
        if engine not in EOL_API_URLS:              # not covered by endoflife.date: dates are kept
            continue
        url = EOL_API_URLS[engine]
        status, text = http_request("GET", url, {"Accept": "application/json"})
        if status != 200:
            raise UpdateError("Couldn't read %s (HTTP %s)." % (url, status))
        try:
            releases = json.loads(text)["result"]["releases"]
        except (ValueError, KeyError, TypeError):
            raise UpdateError("The response from %s wasn't in the expected format." % url)
        if engine == "SQL Server":
            found.update(_endoflife_sql_server(releases))
            continue
        for release in releases:
            version = extract_version(str(release.get("name") or ""), engine)
            end = parse_date(str(release.get("eolFrom") or ""))
            if not version or not end:
                continue
            entry = _new_entry(engine, version)
            entry["standardSupportEnds"] = iso(end)
            entry["extendedSupportEnds"] = iso(parse_date(str(release.get("eoesFrom") or "")))
            found[(engine, version)] = entry
    return found, []


def validate(found):
    """Stop if what was read doesn't look like real support dates."""
    problems = []
    for engine in ENGINES:
        if engine in SERVICE_RETIREMENTS or engine in OPTIONAL_ENGINES:
            continue
        count = sum(1 for (e, _), entry in found.items() if e == engine and entry.get("standardSupportEnds"))
        if count < MIN_VERSIONS_WITH_DATES[engine]:
            problems.append("only %d %s version(s) with an end date were found (at least %d expected)"
                            % (count, engine, MIN_VERSIONS_WITH_DATES[engine]))
    for (engine, version), entry in sorted(found.items(), key=lambda item: _entry_key(item[1])):
        if not _valid_version(version):
            problems.append("%s version %r isn't a version number" % (engine, version))
        dates = {field: from_iso(entry.get(field)) for field in DATE_FIELDS}
        for field, value in dates.items():
            if value and not (EARLIEST_DATE <= value <= LATEST_DATE):
                problems.append("%s %s: %s date %s is outside the expected range"
                                % (engine, version, FIELD_LABELS[field], value.isoformat()))
        if dates["standardSupportEnds"] and dates["extendedSupportEnds"] \
                and dates["extendedSupportEnds"] < dates["standardSupportEnds"]:
            problems.append("%s %s: paid support ends before support ends" % (engine, version))
        if dates["mainstreamSupportEnds"] and dates["standardSupportEnds"] \
                and dates["mainstreamSupportEnds"] > dates["standardSupportEnds"]:
            problems.append("%s %s: mainstream support ends after support ends" % (engine, version))
    if problems:
        raise UpdateError("The dates read don't look right, so nothing was changed:\n  - " + "\n  - ".join(problems))


# --------------------------------------------------------------------------- merging

def merge(previous, found):
    """Combine the dates already in the dashboard with the dates just read."""
    previous_by_key = {(e["engine"], e["version"]): e for e in previous}
    for engine in ENGINES:
        if engine in OPTIONAL_ENGINES:
            continue
        known = [key for key in previous_by_key if key[0] == engine]
        missing = [key for key in known if key not in found]
        if known and len(missing) * 2 > len(known):
            raise UpdateError("%d of the %d %s versions already in the dashboard weren't found in the source "
                              "(%s). Nothing was changed. Check that the source page still lists them."
                              % (len(missing), len(known), engine, ", ".join(v for _, v in missing)))
    merged = []
    for key in set(previous_by_key) | set(found):
        old, new = previous_by_key.get(key), found.get(key)
        if new is None:
            kept = dict(old)
            kept["note"] = _add_note(kept.get("note"), NOTE_NOT_LISTED)
            merged.append(kept)
            continue
        entry = _new_entry(key[0], key[1])
        for field in DATE_FIELDS:
            value = new.get(field)
            if value is None and old is not None:
                value = old.get(field)
            entry[field] = value
        entry["note"] = new.get("note") or ""
        merged.append(entry)
    merged.sort(key=_entry_key)
    return merged


def apply_overrides(entries, overrides):
    """Apply DATE_OVERRIDES from the settings. Returns the keys that were applied."""
    by_key = {(e["engine"], e["version"]): e for e in entries}
    applied = []
    for key, fields in (overrides or {}).items():
        if not isinstance(key, tuple) or len(key) != 2:
            raise UpdateError("DATE_OVERRIDES: use keys like (\"PostgreSQL\", \"14\").")
        engine, version = key[0], str(key[1]).strip()
        if engine not in ENGINES:
            raise UpdateError("DATE_OVERRIDES: unknown engine %r. Use one of: %s."
                              % (engine, ", ".join('"%s"' % e for e in ENGINES)))
        if not _valid_version(version):
            raise UpdateError("DATE_OVERRIDES: %r isn't a version number." % (version,))
        entry = by_key.get((engine, version))
        if entry is None:
            entry = _new_entry(engine, version)
            entries.append(entry)
            by_key[(engine, version)] = entry
        for field, value in (fields or {}).items():
            if field not in DATE_FIELDS:
                raise UpdateError("DATE_OVERRIDES: unknown field %r. Use %s." % (field, ", ".join(DATE_FIELDS)))
            parsed = from_iso(value) if value else None
            if value and parsed is None:
                raise UpdateError("DATE_OVERRIDES: %r isn't a date in YYYY-MM-DD format." % (value,))
            entry[field] = iso(parsed)
        entry["note"] = _add_note(entry.get("note"), NOTE_OVERRIDE)
        applied.append((engine, version))
    entries.sort(key=_entry_key)
    return applied


def describe_changes(previous, final, found):
    previous_by_key = {(e["engine"], e["version"]): e for e in previous}
    rows, changed = [], 0
    for entry in final:
        key = (entry["engine"], entry["version"])
        old = previous_by_key.get(key)
        if old is None:
            status = "added"
            changed += 1
        else:
            fields = [FIELD_LABELS[f] for f in DATE_FIELDS if old.get(f) != entry.get(f)]
            if fields:
                status = "changed: " + ", ".join(fields)
                changed += 1
            else:
                status = "no change"
        if key not in found:
            status += " (not listed by the source)"
        rows.append((entry, status))
    return rows, changed


# --------------------------------------------------------------------------- dashboard content

def build_case_expression(entries, field):
    """The KQL expression the dashboard uses to look up a date for each engine and version."""
    clauses = []
    for engine in ENGINES:
        groups, order = {}, []
        for entry in sorted((e for e in entries if e["engine"] == engine and e.get(field)), key=_entry_key):
            if not _valid_version(entry["version"]) or from_iso(entry[field]) is None:
                raise UpdateError("Unexpected value for %s %s." % (engine, entry["version"]))
            if entry[field] not in groups:
                groups[entry[field]] = []
                order.append(entry[field])
            groups[entry[field]].append(entry["version"])
        every_version = []
        for date_text in order:
            versions = [v for v in groups[date_text] if v != ALL_VERSIONS]
            if len(versions) != len(groups[date_text]):
                every_version.append(date_text)
            if not versions:
                continue
            if len(versions) == 1:
                condition = "MajorVersion == '%s'" % versions[0]
            else:
                condition = "MajorVersion in (%s)" % ", ".join("'%s'" % v for v in versions)
            clauses.append("Engine == '%s' and %s, datetime(%s)" % (engine, condition, date_text))
        for date_text in every_version:                  # whole-service dates come after version dates
            clauses.append("Engine == '%s', datetime(%s)" % (engine, date_text))
    if not clauses:
        return "datetime(null)"
    return "case(" + ", ".join(clauses) + ", datetime(null))"


REFERENCE_COLUMNS = {
    "PostgreSQL": [("standardSupportEnds", "Standard support ends"), ("extendedSupportStarts", "Extended Support starts"),
                   ("extendedSupportEnds", "Extended Support ends")],
    "MySQL": [("standardSupportEnds", "Standard support ends"), ("extendedSupportStarts", "Extended Support starts"),
              ("extendedSupportEnds", "Extended Support ends")],
    "SQL Server": [("mainstreamSupportEnds", "Mainstream support ends"),
                   ("standardSupportEnds", "Extended support ends (free security updates stop)"),
                   ("extendedSupportStarts", "ESU start"), ("extendedSupportEnds", "ESU end")],
    "Cosmos DB for PostgreSQL": [("standardSupportEnds", "Retirement date (Azure)")],
}
REFERENCE_FOOTNOTES = {
    "SQL Server": "Dates as published on Microsoft Lifecycle. After extended support ends, security updates need "
                  "Extended Security Updates (ESU), through Azure Arc, the SQL IaaS Agent extension on Azure VMs, "
                  "or volume licensing. SQL Server 2014 on Azure VMs gets them free.",
    "Cosmos DB for PostgreSQL": "The service itself is on a retirement path and isn't recommended for new projects. "
                                "Microsoft recommends Azure Database for PostgreSQL elastic clusters instead.",
}


def build_reference_markdown(entries, checked, source_label):
    """The 'Support dates' table shown on the dashboard's Lifecycle dates tab."""
    lines = [
        "## Support dates used by this dashboard",
        "Last checked **%s** from %s. The update job refreshes these dates. "
        "A version that isn't listed has no published end date yet." % (display_date(checked), source_label),
        "",
    ]
    for engine in ENGINES:
        if engine in SERVICE_RETIREMENTS:
            continue
        columns = REFERENCE_COLUMNS[engine]
        lines.append("### " + ENGINE_TITLES[engine])
        lines.append("| Version | " + " | ".join(title for _, title in columns) + " | Notes |")
        lines.append("|" + "---|" * (len(columns) + 2))
        rows = sorted((e for e in entries if e["engine"] == engine), key=_entry_key, reverse=True)
        for entry in rows:
            cells = [entry["version"]] + [display_date(entry.get(field)) for field, _ in columns]
            cells.append((entry.get("note") or "").replace("|", "/"))
            lines.append("| " + " | ".join(cells) + " |")
        if not rows:
            lines.append("| " + " | ".join(["", "No dates found"] + [""] * len(columns)) + " |")
        lines.append("")
        if engine in REFERENCE_FOOTNOTES:
            lines.append(REFERENCE_FOOTNOTES[engine])
            lines.append("")
        sources = ["[%s](%s)" % (title, url) for title, url in ENGINE_MICROSOFT_SOURCES[engine]]
        if source_label == SOURCE_LABELS["endoflife.date"] and engine in EOL_PAGE_URLS:
            sources.append("[endoflife.date](%s)" % EOL_PAGE_URLS[engine])
        lines.append("Sources: " + " | ".join(sources))
        lines.append("")
    lines.append("### Services being retired")
    lines.append("These dates apply to every version of the service.")
    lines.append("")
    lines.append("| Service | Retirement date | Notes | Source |")
    lines.append("|---|---|---|---|")
    for engine in ENGINES:
        if engine not in SERVICE_RETIREMENTS:
            continue
        entry = next((e for e in entries if e["engine"] == engine and e["version"] == ALL_VERSIONS), None) or {}
        source = " ".join("[%s](%s)" % (title, url) for title, url in ENGINE_MICROSOFT_SOURCES[engine])
        lines.append("| %s | %s | %s | %s |" % (ENGINE_TITLES[engine], display_date(entry.get("standardSupportEnds")),
                                              (entry.get("note") or "").replace("|", "/"), source))
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def serialize_entries(entries):
    ordered = []
    for entry in sorted(entries, key=_entry_key):
        item = {"engine": entry["engine"], "version": entry["version"]}
        for field in DATE_FIELDS:
            item[field] = entry.get(field)
        item["note"] = entry.get("note") or ""
        ordered.append(item)
    return json.dumps({"schema": 1, "entries": ordered}, separators=(",", ":"), ensure_ascii=False)


def _walk_items(items):
    for item in items or []:
        yield item
        if item.get("type") == 12:
            for child in _walk_items((item.get("content") or {}).get("items")):
                yield child


def find_parameters(workbook):
    params = {}
    for item in _walk_items(workbook.get("items")):
        if item.get("type") == 9:
            for param in (item.get("content") or {}).get("parameters") or []:
                params.setdefault(param.get("name"), param)
    return params


def find_text_item(workbook, name):
    for item in _walk_items(workbook.get("items")):
        if item.get("type") == 1 and item.get("name") == name:
            return item
    return None


def read_dashboard_entries(workbook):
    params = find_parameters(workbook)
    missing = [name for name in REQUIRED_PARAMS if name not in params]
    if missing or find_text_item(workbook, TEXT_ITEM_NAME) is None:
        raise UpdateError("This dashboard doesn't have the settings the update job needs. Install the latest "
                          "dashboard template (delete the old copy first), then run the job again.")
    try:
        data = json.loads(params[PARAM_DATA].get("value") or "")
        entries = []
        for item in data["entries"]:
            entry = _new_entry(str(item["engine"]), str(item["version"]))
            for field in DATE_FIELDS:
                entry[field] = iso(from_iso(item.get(field)))
            entry["note"] = str(item.get("note") or "")
            entries.append(entry)
    except (ValueError, KeyError, TypeError):
        raise UpdateError("The support dates stored in the dashboard couldn't be read. Install the latest "
                          "dashboard template, then run the job again.")
    return entries


def write_dashboard_entries(workbook, entries, checked, source_label):
    params = find_parameters(workbook)
    for field, name in PARAM_EXPRESSIONS.items():
        params[name]["value"] = build_case_expression(entries, field)
    params[PARAM_DATA]["value"] = serialize_entries(entries)
    params[PARAM_CHECKED]["value"] = display_date(checked)
    params[PARAM_CHECKED_ISO]["value"] = checked.isoformat()
    params[PARAM_SOURCE]["value"] = source_label
    find_text_item(workbook, TEXT_ITEM_NAME)["content"]["json"] = build_reference_markdown(entries, checked, source_label)


# --------------------------------------------------------------------------- Azure

def get_access_token():
    """Get a token for Azure Resource Manager from the Automation account's managed identity."""
    query = {"resource": ARM_ENDPOINT + "/"}
    if USER_ASSIGNED_CLIENT_ID.strip():
        query["client_id"] = USER_ASSIGNED_CLIENT_ID.strip()
    endpoint, secret = os.environ.get("IDENTITY_ENDPOINT"), os.environ.get("IDENTITY_HEADER")
    if endpoint and secret:
        url = endpoint + ("&" if "?" in endpoint else "?") + urllib.parse.urlencode(query)
        headers = {"X-IDENTITY-HEADER": secret, "Metadata": "True"}
    else:
        # Hybrid Runbook Worker on an Azure VM: use the VM's managed identity.
        query["api-version"] = "2018-02-01"
        url = "http://169.254.169.254/metadata/identity/oauth2/token?" + urllib.parse.urlencode(query)
        headers = {"Metadata": "true"}
    status, text = http_request("GET", url, headers, timeout=30)
    token = None
    if status == 200:
        try:
            token = json.loads(text).get("access_token")
        except ValueError:
            token = None
    if not token:
        raise UpdateError("Couldn't get a token for the Automation account's managed identity (HTTP %s). Check that "
                          "the identity is turned on under Account Settings > Identity." % status)
    return token


class ArmClient:
    def __init__(self, token):
        self.token = token

    def call(self, method, path, body=None, query=None, api_version=WORKBOOK_API_VERSION):
        if path.startswith("https://"):
            url = path
        else:
            params = {"api-version": api_version}
            params.update(query or {})
            url = ARM_ENDPOINT + path + "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False)
        elif method == "POST":
            data = b""
        status, text = http_request(method, url, headers, data)
        parsed = None
        if text and text.strip()[:1] in ("{", "["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        return status, parsed, text


def _access_problem(status, text, what):
    if status in (401, 403):
        return UpdateError("The Automation account's managed identity isn't allowed to %s. Give it the Workbook "
                           "Contributor role on the dashboard's resource group (it can take a few minutes to "
                           "apply). Details: %s" % (what, _short(text)))
    return UpdateError("Couldn't %s (HTTP %s): %s" % (what, status, _short(text)))


def _kql_string(text):
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"


def find_dashboard(arm):
    """Find the dashboard with Azure Resource Graph: by the template's tag, or else by name."""
    names = [WORKBOOK_NAME.strip()] + [n for n in LEGACY_WORKBOOK_NAMES if n.lower() != WORKBOOK_NAME.strip().lower()]
    query = "\n".join([
        "resources",
        "| where type =~ 'microsoft.insights/workbooks'",
        "| where tostring(tags['%s']) =~ 'true' or tostring(properties.displayName) in~ (%s)"
        % (DASHBOARD_TAG, ", ".join(_kql_string(n) for n in names)),
        "| project id, displayName = tostring(properties.displayName), tagged = tostring(tags['%s'])" % DASHBOARD_TAG,
        "| order by id asc",
    ])
    rows = query_resource_graph(arm, query)
    tagged = [row for row in rows if str(row.get("tagged") or "").lower() == "true"]
    candidates = tagged or rows
    if len(candidates) == 1:
        return str(candidates[0]["id"])
    if not candidates:
        raise UpdateError("Couldn't find the dashboard. Check that it's deployed and that the Automation account's managed "
                          "identity has Workbook Contributor on its resource group (it can take a few minutes to apply). "
                          "Or set WORKBOOK_RESOURCE_ID in the SETTINGS section.")
    raise UpdateError("Found %d copies of the dashboard, so the job can't tell which one to update:\n  - %s\nDelete the "
                      "copies you don't use, or set WORKBOOK_RESOURCE_ID in the SETTINGS section."
                      % (len(candidates), "\n  - ".join(str(row["id"]) for row in candidates)))


def resolve_workbook_id(arm):
    if WORKBOOK_RESOURCE_ID.strip():
        return WORKBOOK_RESOURCE_ID.strip()
    if not SUBSCRIPTION_ID.strip() and not RESOURCE_GROUP.strip():
        return find_dashboard(arm)
    path = "/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Insights/workbooks" % (
        urllib.parse.quote(SUBSCRIPTION_ID.strip()), urllib.parse.quote(RESOURCE_GROUP.strip()))
    status, data, text = arm.call("GET", path, query={"category": "workbook"})
    matches = []
    while True:
        if status == 404:
            raise UpdateError("Resource group %s wasn't found in subscription %s. Check the SETTINGS section."
                              % (RESOURCE_GROUP, SUBSCRIPTION_ID))
        if status != 200 or data is None:
            raise _access_problem(status, text, "list the workbooks in resource group %s" % RESOURCE_GROUP)
        for workbook in data.get("value") or []:
            name = ((workbook.get("properties") or {}).get("displayName") or "").strip().lower()
            if name == WORKBOOK_NAME.strip().lower():
                matches.append(workbook["id"])
        next_link = data.get("nextLink")
        if not next_link:
            break
        status, data, text = arm.call("GET", next_link)
    if not matches:
        raise UpdateError("No dashboard named \"%s\" was found in resource group %s. Check WORKBOOK_NAME and "
                          "RESOURCE_GROUP in the SETTINGS section." % (WORKBOOK_NAME, RESOURCE_GROUP))
    if len(matches) > 1:
        raise UpdateError("%d dashboards named \"%s\" were found in resource group %s. Delete the old copy, or set "
                          "WORKBOOK_RESOURCE_ID in the SETTINGS section." % (len(matches), WORKBOOK_NAME, RESOURCE_GROUP))
    return matches[0]


def load_workbook(arm, workbook_id):
    status, resource, text = arm.call("GET", workbook_id, query={"canFetchContent": "true"})
    if status == 404:
        raise UpdateError("The dashboard %s wasn't found." % workbook_id)
    if status != 200 or resource is None:
        raise _access_problem(status, text, "read the dashboard")
    try:
        workbook = json.loads(resource["properties"]["serializedData"])
    except (ValueError, KeyError, TypeError):
        raise UpdateError("The dashboard's content couldn't be read.")
    return resource, workbook


def save_workbook(arm, workbook_id, resource, workbook):
    props = resource.get("properties") or {}
    body = {
        "location": resource["location"],
        "kind": resource.get("kind") or "shared",
        "tags": resource.get("tags") or {},
        "properties": {
            "displayName": props["displayName"],
            "serializedData": json.dumps(workbook, ensure_ascii=False, separators=(",", ":")),
            "category": props.get("category") or "workbook",
            "version": props.get("version") or "1.0",
            "sourceId": props.get("sourceId") or "azure monitor",
        },
    }
    if props.get("description"):
        body["properties"]["description"] = props["description"]
    if props.get("storageUri"):
        body["properties"]["storageUri"] = props["storageUri"]
    identity = resource.get("identity") or {}
    if identity.get("type") and identity.get("type") != "None":
        body["identity"] = {"type": identity["type"]}
        if identity.get("userAssignedIdentities"):
            body["identity"]["userAssignedIdentities"] = {k: {} for k in identity["userAssignedIdentities"]}
    status, _, text = arm.call("PUT", workbook_id, body=body)
    if status not in (200, 201):
        raise _access_problem(status, text, "save the dashboard")


# --------------------------------------------------------------------------- helpers

def _now_utc():
    if _now_override is not None:
        return _now_override
    return datetime.datetime.now(datetime.timezone.utc)


def _today():
    if _today_override is not None:
        return _today_override
    return _now_utc().date()


def _normalize_source(value):
    value = (value or "").strip().lower()
    if value in ("microsoft-learn", "microsoft learn", "learn", "microsoft", "mslearn"):
        return "microsoft-learn"
    if value in ("endoflife.date", "endoflife", "eol", "endoflifedate"):
        return "endoflife.date"
    raise UpdateError("Unknown source %r. Use \"microsoft-learn\" or \"endoflife.date\"." % value)


def print_report(rows, checked, source_label):
    log("")
    log("Support dates checked %s from %s:" % (display_date(checked), source_label))
    header = ("Engine", "Version", "Support ends", "Mainstream ends", "Paid support starts", "Paid support ends",
              "Change")
    table = [header]
    for entry, status in rows:
        table.append((entry["engine"], entry["version"]) + tuple(display_date(entry.get(f)) or "-" for f in DATE_FIELDS)
                     + (status,))
    widths = [max(len(str(row[i])) for row in table) for i in range(len(header))]
    for row in table:
        log("  " + "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    for entry, _ in rows:
        if entry.get("note"):
            log("  Note - %s %s: %s" % (entry["engine"], entry["version"], entry["note"]))
    log("")


# --------------------------------------------------------------------------- reminders

ARG_API_VERSION = "2024-04-01"
LOGIC_API_VERSION = "2019-05-01"
LOGIC_TRIGGER_NAME = "manual"
PARAM_RED_DAYS = "RedDays"
PARAM_AMBER_DAYS = "AmberDays"
PARAM_OWNER_TAG = "OwnerTag"
DEFAULT_RED_DAYS = 180
DEFAULT_AMBER_DAYS = 365
DEFAULT_OWNER_TAG = "Owner"
MAX_ROWS_PER_EMAIL = 200
MAX_GRAPH_PAGES = 100

STATUS_PAST = "Past end of support"
STATUS_RED = "Red"
STATUS_AMBER = "Amber"
STATUS_GREEN = "Green"
STATUS_NO_DATE = "No end date"
STATUS_ORDER = (STATUS_PAST, STATUS_RED, STATUS_AMBER, STATUS_GREEN, STATUS_NO_DATE)
STATUS_STYLES = {
    STATUS_PAST: "background:#efe3f5;color:#5c2d73;font-weight:600",
    STATUS_RED: "background:#fde7e9;color:#a4262c;font-weight:600",
}

# Extended Security Updates (ESU) status of a SQL Server. The dashboard shows the same values.
ESU_SUBSCRIBED = "Subscribed (Azure Arc)"
ESU_NOT_SUBSCRIBED = "Not subscribed (Azure Arc)"
ESU_FREE = "Free on Azure VMs"
ESU_ENDED = "ESU period ended"
ESU_NOT_REPORTED = "Not reported"

# "What to do" text. The dashboard uses the same text, so it has no apostrophes.
NOTE_PAST = ("In Extended Support, which is charged after any grace period. "
             "Upgrade to a supported version to stop the charges.")
NOTE_RED = "Standard support ends soon. Schedule the major version upgrade now."
SQL_NOTE_PAST_ENDED = "No more security updates, even with Extended Security Updates. Upgrade or migrate now."
SQL_NOTE_PAST_SUBSCRIBED = ("Covered by Extended Security Updates until the paid support end date. "
                            "Plan the upgrade before then.")
SQL_NOTE_PAST_FREE = ("Free Extended Security Updates on Azure VMs until the paid support end date. "
                      "Keep Windows Update on and plan the upgrade.")
SQL_NOTE_PAST_NOT_COVERED = ("No security updates unless Extended Security Updates are bought (through Azure Arc, "
                             "the SQL IaaS Agent extension or volume licensing). Upgrade, or subscribe to ESU.")
SQL_NOTE_RED = "Free security updates end soon. Schedule the upgrade, or plan for Extended Security Updates."

HOSTING_AZURE = "Azure managed service"
HOSTING_ARC = "Azure Arc"
HOSTING_ARC_DISCONNECTED = "Azure Arc (disconnected)"
HOSTING_VM = "Azure VM"

SQL_ARC_TYPE = "microsoft.azurearcdata/sqlserverinstances"
SQL_VM_TYPE = "microsoft.sqlvirtualmachine/sqlvirtualmachines"
SERVER_TYPES = {
    "microsoft.dbforpostgresql/flexibleservers": "PostgreSQL",
    "microsoft.dbformysql/flexibleservers": "MySQL",
    SQL_ARC_TYPE: "SQL Server",
    SQL_VM_TYPE: "SQL Server",
    "microsoft.dbforpostgresql/servergroupsv2": "Cosmos DB for PostgreSQL",
    "microsoft.dbforpostgresql/servers": "PostgreSQL single server",
    "microsoft.dbformysql/servers": "MySQL single server",
    "microsoft.dbformariadb/servers": "MariaDB",
    "microsoft.cache/redis": "Azure Cache for Redis",
    "microsoft.cache/redisenterprise": "Azure Cache for Redis Enterprise",   # Enterprise tiers only (see the query)
}
# What to do for services that don't have version notes of their own. No apostrophes: the dashboard uses the same text.
SERVICE_NOTES = {
    "PostgreSQL single server": "This service has been retired. Migrate to Azure Database for PostgreSQL flexible server.",
    "MySQL single server": "This service has been retired. Migrate to Azure Database for MySQL flexible server.",
    "MariaDB": "This service has been retired. Migrate to Azure Database for MySQL flexible server.",
    "Azure Cache for Redis": "This service is being retired. Plan the move to Azure Managed Redis before the date shown.",
    "Azure Cache for Redis Enterprise": ("The Enterprise tiers are being retired. Plan the move to Azure Managed Redis "
                                        "before the date shown, after which remaining caches are moved automatically."),
}
COSMOS_PG_NOTES = {
    STATUS_PAST: ("This PostgreSQL version is retired, so the engine gets no more fixes. Upgrade it, and plan a move to "
                  "Azure Database for PostgreSQL elastic clusters, as this service is on a retirement path."),
    STATUS_RED: ("This PostgreSQL version retires soon. Upgrade it, and plan a move to Azure Database for PostgreSQL "
                 "elastic clusters, as this service is on a retirement path."),
    "": "This service is on a retirement path. Plan a move to Azure Database for PostgreSQL elastic clusters.",
}
SERVERS_QUERY = "\n".join([
    "resources",
    "| where type in~ ('microsoft.dbforpostgresql/flexibleservers', 'microsoft.dbformysql/flexibleservers', "
    "'microsoft.sqlvirtualmachine/sqlvirtualmachines', 'microsoft.dbforpostgresql/servergroupsv2', "
    "'microsoft.dbforpostgresql/servers', 'microsoft.dbformysql/servers', 'microsoft.dbformariadb/servers', "
    "'microsoft.cache/redis')",
    "    or (type =~ 'microsoft.cache/redisenterprise' and tostring(sku.name) startswith 'Enterprise')",
    "    or (type =~ 'microsoft.azurearcdata/sqlserverinstances' and (isempty(tostring(properties.serviceType)) "
    "or tostring(properties.serviceType) =~ 'Engine'))",
    "| project id, name, type = tolower(type), subscriptionId, resourceGroup, location, tags,",
    "    version = case(type =~ 'microsoft.sqlvirtualmachine/sqlvirtualmachines', tostring(properties.sqlImageOffer), "
    "type =~ 'microsoft.dbforpostgresql/servergroupsv2', tostring(properties.postgresqlVersion), "
    "type in~ ('microsoft.cache/redis', 'microsoft.cache/redisenterprise'), tostring(properties.redisVersion), "
    "tostring(properties.version)),",
    "    edition = iff(type =~ 'microsoft.sqlvirtualmachine/sqlvirtualmachines', tostring(properties.sqlImageSku), "
    "tostring(properties.edition)),",
    "    hostId = tolower(case(type =~ 'microsoft.azurearcdata/sqlserverinstances', "
    "tostring(properties.containerResourceId), type =~ 'microsoft.sqlvirtualmachine/sqlvirtualmachines', "
    "tostring(properties.virtualMachineResourceId), '')),",
    "    serviceType = tostring(properties.serviceType), connection = tostring(properties.status)",
    "| order by id asc",
])
# The ESU setting of each Arc-enabled server, from its SQL Server extension.
SQL_ESU_QUERY = "\n".join([
    "resources",
    "| where type =~ 'microsoft.hybridcompute/machines/extensions'",
    "| where tostring(properties.type) in~ ('WindowsAgent.SqlServer', 'LinuxAgent.SqlServer')",
    "| project id, hostId = tolower(substring(id, 0, indexof(id, '/extensions/'))), "
    "esu = tolower(coalesce(tostring(properties.settings.enableExtendedSecurityUpdates), "
    "tostring(properties.settings.EnableExtendedSecurityUpdates)))",
    "| order by id asc",
])
# Tags of the machines and VMs that host SQL Server, used when the SQL Server itself has no owner tag.
SQL_HOSTS_QUERY = "\n".join([
    "resources",
    "| where type in~ ('microsoft.hybridcompute/machines', 'microsoft.compute/virtualmachines')",
    "| where isnotnull(tags)",
    "| project id, hostId = tolower(id), tags",
    "| order by id asc",
])
SUBSCRIPTIONS_QUERY = "\n".join([
    "resourcecontainers",
    "| where type =~ 'microsoft.resources/subscriptions'",
    "| project id, subscriptionId, name",
    "| order by id asc",
])
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
CELL_STYLE = "border:1px solid #d1d1d1;padding:4px 8px;text-align:left;vertical-align:top"


def parse_emails(text, where):
    """Email addresses separated by semicolons, commas or spaces. Stops on anything that isn't an address."""
    tokens = [token for token in re.split(r"[;,\s]+", text or "") if token]
    bad = [token for token in tokens if not EMAIL_PATTERN.fullmatch(token)]
    if bad:
        raise UpdateError("These entries in %s aren't email addresses: %s" % (where, ", ".join(bad)))
    team = []
    for token in tokens:
        if token.lower() not in team:
            team.append(token.lower())
    return team


def reminder_recipients():
    """The TEAM_EMAILS setting in this file, checked before anything else runs. Usually empty."""
    if not SEND_REMINDERS:
        return []
    return parse_emails(TEAM_EMAILS, "TEAM_EMAILS")


def reminder_targets(workbook, team_from_settings):
    """Who gets the summary and which Logic App sends it. The dashboard's settings win over this file's."""
    params = find_parameters(workbook)
    in_dashboard = str((params.get(PARAM_TEAM_EMAILS) or {}).get("value") or "").strip()
    team = parse_emails(in_dashboard, "the dashboard's Reminder emails to setting") if in_dashboard else team_from_settings
    logic_app = str((params.get(PARAM_LOGIC_APP) or {}).get("value") or "").strip() or LOGIC_APP_NAME.strip()
    return team, logic_app


def reminder_settings(workbook):
    """The Red and Amber thresholds and the owner tag name, as saved in the dashboard."""
    params = find_parameters(workbook)

    def number(name, default):
        try:
            value = int(str((params.get(name) or {}).get("value", "")).strip())
        except ValueError:
            return default
        return value if value >= 0 else default

    owner_tag = str((params.get(PARAM_OWNER_TAG) or {}).get("value") or "").strip() or DEFAULT_OWNER_TAG
    return number(PARAM_RED_DAYS, DEFAULT_RED_DAYS), number(PARAM_AMBER_DAYS, DEFAULT_AMBER_DAYS), owner_tag


def query_resource_graph(arm, query):
    """Run an Azure Resource Graph query across every subscription the identity can read."""
    rows, skip_token = [], None
    for _ in range(MAX_GRAPH_PAGES):
        options = {"resultFormat": "objectArray", "$top": 1000}
        if skip_token:
            options["$skipToken"] = skip_token
        status, data, text = arm.call("POST", "/providers/Microsoft.ResourceGraph/resources",
                                      body={"query": query, "options": options}, api_version=ARG_API_VERSION)
        if status in (401, 403):
            raise UpdateError("The Automation account's managed identity isn't allowed to query Azure Resource Graph. "
                              "Give it the Reader role on the subscriptions that have database servers. Details: %s"
                              % _short(text))
        if status != 200 or not isinstance(data, dict):
            raise UpdateError("Couldn't query Azure Resource Graph (HTTP %s): %s" % (status, _short(text)))
        rows.extend(data.get("data") or [])
        skip_token = data.get("$skipToken")
        if not skip_token:
            return rows
    raise UpdateError("Azure Resource Graph returned more than %d pages of results." % MAX_GRAPH_PAGES)


def _tag_value(tags, name):
    """A tag's value, matching the tag name without regard to case."""
    if not isinstance(tags, dict):
        return ""
    for key, value in tags.items():
        if str(key).strip().lower() == name.lower():
            return str(value or "").strip()
    return ""


def major_version(engine, row_type, raw):
    """The version the dates are looked up by, worked out the same way as in the dashboard."""
    if engine in ("MySQL", "Azure Cache for Redis", "Azure Cache for Redis Enterprise") and "." in raw:
        parts = raw.split(".")
        return parts[0] + "." + parts[1]
    if row_type == SQL_ARC_TYPE and raw.lower().startswith("sql server "):
        return raw[11:]                                   # "SQL Server 2019" -> "2019"
    if row_type == SQL_VM_TYPE and raw.lower().startswith("sql"):
        return raw[3:7] + (" R2" if raw[7:9].lower() == "r2" else "")   # "SQL2019-WS2022" -> "2019"
    return raw


def _midnight(date_value):
    return datetime.datetime(date_value.year, date_value.month, date_value.day, tzinfo=datetime.timezone.utc)


def esu_status(row_type, version, entry, esu_setting, now):
    """Extended Security Updates status of a SQL Server, the same way the dashboard works it out."""
    esu_end = from_iso(entry.get("extendedSupportEnds"))
    if esu_end is None:
        return ""
    if _midnight(esu_end) < now:
        return ESU_ENDED
    if row_type == SQL_ARC_TYPE:
        if esu_setting == "true":
            return ESU_SUBSCRIBED
        return ESU_NOT_SUBSCRIBED if esu_setting else ESU_NOT_REPORTED
    if row_type == SQL_VM_TYPE and version in SQL_FREE_ESU_ON_AZURE_VMS:
        return ESU_FREE
    return ESU_NOT_REPORTED


def next_step(engine, status, esu):
    if engine in SERVICE_NOTES:
        return SERVICE_NOTES[engine]
    if engine == "Cosmos DB for PostgreSQL":
        return COSMOS_PG_NOTES.get(status, COSMOS_PG_NOTES[""])
    if engine != "SQL Server":
        return {STATUS_PAST: NOTE_PAST, STATUS_RED: NOTE_RED}.get(status, "")
    if status == STATUS_PAST:
        if esu in ("", ESU_ENDED):
            return SQL_NOTE_PAST_ENDED
        if esu == ESU_SUBSCRIBED:
            return SQL_NOTE_PAST_SUBSCRIBED
        if esu == ESU_FREE:
            return SQL_NOTE_PAST_FREE
        return SQL_NOTE_PAST_NOT_COVERED
    if status == STATUS_RED:
        return SQL_NOTE_RED
    return ""


def evaluate_servers(rows, entries, red_days, amber_days, owner_tag, subscription_names, now,
                     esu_by_host=None, host_tags=None):
    """Work out each server's status the same way the dashboard does."""
    lookup = {(entry["engine"], entry["version"]): entry for entry in entries}
    servers = []
    for row in rows:
        row_type = str(row.get("type") or "").lower()
        engine = SERVER_TYPES.get(row_type)
        if not engine:
            continue
        service = str(row.get("serviceType") or "").strip().lower()
        if row_type == SQL_ARC_TYPE and service not in ("", "engine"):
            continue                                      # Reporting Services and other associated services
        raw_version = str(row.get("version") or "").strip()
        version = major_version(engine, row_type, raw_version)
        entry = lookup.get((engine, version)) or lookup.get((engine, ALL_VERSIONS)) or {}
        end = from_iso(entry.get("standardSupportEnds"))
        days_left = None
        if end is None:
            status = STATUS_NO_DATE
        else:
            days_left = int((_midnight(end) - now).total_seconds() / 86400)   # same rounding as the dashboard
            if days_left < 0:
                status = STATUS_PAST
            elif days_left <= red_days:
                status = STATUS_RED
            elif days_left <= amber_days:
                status = STATUS_AMBER
            else:
                status = STATUS_GREEN
        host_id = str(row.get("hostId") or "").lower()
        if row_type == SQL_ARC_TYPE:
            disconnected = str(row.get("connection") or "").strip().lower() == "disconnected"
            hosting = HOSTING_ARC_DISCONNECTED if disconnected else HOSTING_ARC
        elif row_type == SQL_VM_TYPE:
            hosting = HOSTING_VM
        else:
            hosting = HOSTING_AZURE
        esu = ""
        if engine == "SQL Server":
            esu = esu_status(row_type, version, entry, (esu_by_host or {}).get(host_id, ""), now)
        owner = _tag_value(row.get("tags"), owner_tag)
        if not owner and engine == "SQL Server":
            owner = _tag_value((host_tags or {}).get(host_id), owner_tag)
        owner_emails = []
        for address in EMAIL_PATTERN.findall(owner):
            if address.lower() not in owner_emails:
                owner_emails.append(address.lower())
        edition = str(row.get("edition") or "").strip() if engine == "SQL Server" else ""
        subscription_id = str(row.get("subscriptionId") or "")
        servers.append({
            "id": str(row.get("id") or ""),
            "name": str(row.get("name") or ""),
            "engine": engine,
            "version": version or "Not reported",
            "edition": edition,
            "hosting": hosting,
            "status": status,
            "daysLeft": days_left,
            "standardSupportEnds": entry.get("standardSupportEnds"),
            "mainstreamSupportEnds": entry.get("mainstreamSupportEnds"),
            "extendedSupportStarts": entry.get("extendedSupportStarts"),
            "extendedSupportEnds": entry.get("extendedSupportEnds"),
            "esu": esu,
            "next": next_step(engine, status, esu),
            "owner": owner,
            "ownerEmails": owner_emails,
            "subscription": subscription_names.get(subscription_id) or subscription_id,
            "resourceGroup": str(row.get("resourceGroup") or ""),
        })
    servers.sort(key=lambda s: (s["daysLeft"] is None, s["daysLeft"] if s["daysLeft"] is not None else 0,
                                s["name"].lower()))
    return servers


def portal_link(resource_id):
    return "https://portal.azure.com/#resource" + resource_id


def _plural(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def _days_text(days):
    if days is None:
        return ""
    if days < 0:
        return "Ended %s ago" % _plural(-days, "day")
    return str(days)


def _extended_text(server):
    return display_date(server.get("extendedSupportEnds"))


def build_email(kind, recipients, servers, red_days, owner_tag, dates_line, dashboard_link):
    """Subject and HTML body for one reminder email."""
    count = len(servers)
    has_past = any(s["status"] == STATUS_PAST for s in servers)
    window = "near or past end of support" if INCLUDE_PAST_SUPPORT else "near end of support"
    verb = "is" if count == 1 else "are"
    has_sql = any(s["engine"] == "SQL Server" for s in servers)
    if kind == "team":
        subject = "Action needed: %s %s %s" % (_plural(count, "database server"), verb, window)
        opening = ""
    else:
        if count == 1:
            subject = "Action needed: your database server is %s" % window
        else:
            subject = "Action needed: %d of your database servers are %s" % (count, window)
        opening = "You're listed as the owner of %s. " % ("this server" if count == 1 else "these servers")
    reaches = "This database server reaches" if count == 1 else "These %d database servers reach" % count
    passed = (", or has already passed it" if count == 1 else ", or have already passed it") if INCLUDE_PAST_SUPPORT else ""
    intro = "%s%s the end of support within %d days%s. Please plan the upgrades." % (
        opening, reaches, red_days, passed)

    def esc(value):
        return html.escape(str(value or ""), quote=True)

    headers = ["Server", "Status", "Engine", "Version"]
    if has_sql:
        headers.append("Hosting")
    headers += ["Support ends", "Days left", "Paid support ends"]
    if has_sql:
        headers.append("ESU")
    headers += ["Subscription", "Resource group"]
    include_owner = kind == "team"
    if include_owner:
        headers.append("Owner")
    headers.append("What to do")
    parts = [
        '<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#242424">',
        "<p>Hello,</p>",
        "<p>%s</p>" % esc(intro),
        '<table style="border-collapse:collapse;font-size:13px">',
        "<tr>" + "".join('<th style="%s;background:#f3f2f1">%s</th>' % (CELL_STYLE, esc(h)) for h in headers) + "</tr>",
    ]
    for server in servers[:MAX_ROWS_PER_EMAIL]:
        cells = [
            '<td style="%s"><a href="%s">%s</a></td>' % (CELL_STYLE, esc(portal_link(server["id"])), esc(server["name"])),
            '<td style="%s;%s">%s</td>' % (CELL_STYLE, STATUS_STYLES.get(server["status"], ""), esc(server["status"])),
        ]
        version = server["version"] + (" " + server["edition"] if server.get("edition") else "")
        values = [server["engine"], version]
        if has_sql:
            values.append(server["hosting"])
        values += [display_date(server.get("standardSupportEnds")), _days_text(server["daysLeft"]),
                   _extended_text(server)]
        if has_sql:
            values.append(server["esu"])
        values += [server["subscription"], server["resourceGroup"]]
        if include_owner:
            values.append(server["owner"])
        values.append(server["next"])
        cells.extend('<td style="%s">%s</td>' % (CELL_STYLE, esc(v)) for v in values)
        parts.append("<tr>" + "".join(cells) + "</tr>")
    parts.append("</table>")
    if count > MAX_ROWS_PER_EMAIL:
        parts.append("<p>...and %s more. See the dashboard for the full list.</p>"
                     % _plural(count - MAX_ROWS_PER_EMAIL, "server"))
    if has_sql:
        parts.append("<p>%s</p>" % esc(
            "For SQL Server, support ends when Microsoft's extended support ends and free security updates stop. "
            "After that, security updates need Extended Security Updates (ESU) until the Paid support ends date."))
    parts.append("<p>%s</p>" % esc(dates_line))
    parts.append('<p><a href="%s">Open the database end-of-support dashboard in the Azure portal</a></p>'
                 % esc(dashboard_link))
    footer = ("This reminder is sent every week while servers are within %d days of the end of support%s. "
              "Owners are taken from the \"%s\" tag on each server%s." % (
                  red_days, " or past it" if INCLUDE_PAST_SUPPORT else "", owner_tag,
                  " (for SQL Server, on the instance or else on its machine or VM)" if has_sql else ""))
    parts.append('<p style="color:#616161;font-size:12px">%s</p>' % esc(footer))
    parts.append("</div>")
    return {
        "kind": kind,
        "to": ";".join(recipients),
        "subject": subject,
        "body": "\n".join(parts),
        "importance": "High" if has_past else "Normal",
        "count": count,
    }


def build_messages(due, team, red_days, owner_tag, dates_line, dashboard_link):
    """One summary for the database team, plus one email per owner address."""
    messages = [build_email("team", team, due, red_days, owner_tag, dates_line, dashboard_link)]
    if EMAIL_OWNERS:
        by_owner = {}
        for server in due:
            for address in server["ownerEmails"]:
                if address not in team:
                    by_owner.setdefault(address, []).append(server)
        for address in sorted(by_owner):
            messages.append(build_email("owner", [address], by_owner[address], red_days, owner_tag,
                                        dates_line, dashboard_link))
    return messages


def get_logic_app_url(arm, workbook_id, logic_app):
    """Ask Azure for the Logic App's trigger address. It's never stored or printed."""
    match = re.match(r"(?i)^/subscriptions/([^/]+)/resourcegroups/([^/]+)/", workbook_id)
    if not match:
        raise UpdateError("Couldn't work out the dashboard's resource group from %s." % workbook_id)
    name = logic_app
    path = "/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Logic/workflows/%s/triggers/%s/listCallbackUrl" % (
        match.group(1), match.group(2), urllib.parse.quote(name), LOGIC_TRIGGER_NAME)
    status, data, text = arm.call("POST", path, api_version=LOGIC_API_VERSION)
    if status == 404:
        raise UpdateError("No Logic App named \"%s\" was found in resource group %s. Deploy the latest dashboard "
                          "template, or check the Logic App name the dashboard uses." % (name, match.group(2)))
    if status in (401, 403):
        raise UpdateError("The Automation account's managed identity isn't allowed to use the Logic App \"%s\". Give it "
                          "the Logic App Contributor role on that Logic App (the dashboard template does this when you "
                          "enter the identity's principal ID). It can take a few minutes to apply. Details: %s"
                          % (name, _short(text)))
    if status != 200 or not isinstance(data, dict) or not data.get("value"):
        raise UpdateError("Couldn't get the Logic App's address (HTTP %s): %s" % (status, _short(text)))
    return data["value"]


def send_email(url, message):
    payload = {"to": message["to"], "subject": message["subject"], "body": message["body"],
               "importance": message["importance"]}
    # Only throttling is retried, so a slow reply never leads to a duplicate email.
    status, text = http_request("POST", url, {"Content-Type": "application/json", "Accept": "application/json"},
                                json.dumps(payload, ensure_ascii=False), timeout=150, attempts=3,
                                retry_statuses=(429,))
    if status == 200:
        return
    if status in (502, 504):
        hint = ("The Logic App didn't reply in time, so the email may or may not have been sent. "
                "Check the Logic App's run history.")
    elif status in (401, 403, 404):
        hint = "The Logic App refused the request. Check that it's enabled, then run the job again."
    else:
        hint = ("Most often the Outlook connection hasn't been authorized yet, or the signed-in account isn't "
                "allowed to send as the sender mailbox. The Logic App's run history has the details.")
    raise UpdateError("The Logic App couldn't send the email to %s (HTTP %s). %s Details: %s"
                      % (message["to"], status, hint, _short(text, 600)))


def send_reminders(arm, workbook_id, entries, settings, team, dates_line, dry_run, logic_app=None):
    red_days, amber_days, owner_tag = settings
    log("")
    log("Checking database servers for reminders (Red = support ends within %d days)..." % red_days)
    subscriptions = query_resource_graph(arm, SUBSCRIPTIONS_QUERY)
    if not subscriptions:
        raise UpdateError("The Automation account's managed identity can't see any subscriptions, so it can't check "
                          "the database servers. Give it the Reader role on each subscription that has database "
                          "servers, or on a management group above them.")
    names = {str(row.get("subscriptionId") or ""): str(row.get("name") or "") for row in subscriptions}
    rows = query_resource_graph(arm, SERVERS_QUERY)
    esu_by_host, host_tags = {}, {}
    if any(str(row.get("type") or "").lower() == SQL_ARC_TYPE for row in rows):
        try:
            for row in query_resource_graph(arm, SQL_ESU_QUERY):
                esu_by_host[str(row.get("hostId") or "").lower()] = str(row.get("esu") or "").strip().lower()
        except UpdateError as error:
            log("WARNING: Couldn't read the Extended Security Updates settings of the Arc-enabled servers, so ESU "
                "shows as not reported. %s" % error)
    if any(str(row.get("type") or "").lower() in (SQL_ARC_TYPE, SQL_VM_TYPE)
           and not _tag_value(row.get("tags"), owner_tag) for row in rows):
        try:
            for row in query_resource_graph(arm, SQL_HOSTS_QUERY):
                host_tags[str(row.get("hostId") or "").lower()] = row.get("tags")
        except UpdateError as error:
            log("WARNING: Couldn't read the tags of the machines and VMs that run SQL Server, so only the SQL Server "
                "resources' own owner tags were used. %s" % error)
    servers = evaluate_servers(rows, entries, red_days, amber_days, owner_tag, names, _now_utc(),
                               esu_by_host, host_tags)
    counts = {}
    for server in servers:
        counts[server["status"]] = counts.get(server["status"], 0) + 1
    summary = ", ".join("%d %s" % (counts[s], s) for s in STATUS_ORDER if counts.get(s))
    engines = ", ".join("%d %s" % (n, engine) for engine, n in
                        ((e, sum(1 for s in servers if s["engine"] == e)) for e in ENGINES) if n)
    log("Checked %s in %s%s: %s." % (_plural(len(servers), "database server"),
                                      _plural(len(subscriptions), "subscription"),
                                      " (%s)" % engines if engines else "", summary or "none found"))
    due = [s for s in servers if s["status"] == STATUS_RED or (INCLUDE_PAST_SUPPORT and s["status"] == STATUS_PAST)]
    if not due:
        log("No servers need a reminder, so no emails were sent.")
        return
    messages = build_messages(due, team, red_days, owner_tag, dates_line, portal_link(workbook_id))
    for message in messages:
        label = "database team summary" if message["kind"] == "team" else "owner"
        log("  Reminder to %s (%s): %s" % (message["to"], label, _plural(message["count"], "server")))
    if dry_run:
        log("Dry run: no reminder emails were sent.")
        return
    url = get_logic_app_url(arm, workbook_id, (logic_app or LOGIC_APP_NAME).strip())
    failures = []
    for message in messages:
        try:
            send_email(url, message)
        except UpdateError as error:
            failures.append(str(error))
    log("Sent %s." % _plural(len(messages) - len(failures), "reminder email"))
    if failures:
        raise UpdateError("%d of %d reminder emails couldn't be sent:\n  - %s"
                          % (len(failures), len(messages), "\n  - ".join(failures)))


# --------------------------------------------------------------------------- main

def refresh_dates(arm, workbook_id, resource, workbook, previous, source, source_label, checked, dry_run):
    """Read, check and save the support dates. Returns the dates the dashboard now uses."""
    log("Reading support dates from %s..." % source_label)
    found, warnings = read_dates(source)
    validate(found)
    counts = ["%d %s" % (sum(1 for key in found if key[0] == engine), engine) for engine in ENGINES
              if engine not in SERVICE_RETIREMENTS]
    log("Found end dates for %s versions, and %d service retirement dates."
        % (", ".join(counts[:-1]) + " and " + counts[-1], len(SERVICE_RETIREMENTS)))
    final = merge(previous, found)
    applied = apply_overrides(final, DATE_OVERRIDES)
    rows, changed = describe_changes(previous, final, found)
    print_report(rows, checked, source_label)
    for key in applied:
        log("Manual date from DATE_OVERRIDES applied: %s %s" % key)
    for warning in warnings:
        log("WARNING: " + warning)
    if dry_run:
        log("Dry run: nothing was saved. %d version(s) would change." % changed)
        return final
    write_dashboard_entries(workbook, final, checked, source_label)
    save_workbook(arm, workbook_id, resource, workbook)
    # Read the dashboard back to confirm the save worked.
    _, saved = load_workbook(arm, workbook_id)
    if read_dashboard_entries(saved) != read_dashboard_entries(workbook):
        raise UpdateError("The dashboard was saved, but reading it back didn't show the new dates. Run the job again.")
    log("Dashboard updated: %d version(s) changed; dates marked as checked on %s." % (changed, display_date(checked)))
    return final


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    dry_run, source = DRY_RUN, SOURCE
    for arg in args:
        value = str(arg).strip()
        lowered = value.lower()
        if not value:
            continue
        if lowered in ("--dry-run", "--dryrun", "dry-run", "dryrun", "-dryrun"):
            dry_run = True
        elif lowered.startswith("--source="):
            source = value.split("=", 1)[1]
        else:
            log("WARNING: ignoring unknown argument %r (use --dry-run or --source=endoflife.date)." % value)
    source = _normalize_source(source)
    source_label = SOURCE_LABELS[source]
    checked = _today()
    if not WORKBOOK_RESOURCE_ID.strip() and bool(SUBSCRIPTION_ID.strip()) != bool(RESOURCE_GROUP.strip()):
        raise UpdateError("Fill in both SUBSCRIPTION_ID and RESOURCE_GROUP in the SETTINGS section, or leave both empty "
                          "so the job finds the dashboard by itself.")
    team_from_settings = reminder_recipients()

    arm = ArmClient(get_access_token())
    workbook_id = resolve_workbook_id(arm)
    log("Dashboard: %s" % workbook_id)
    resource, workbook = load_workbook(arm, workbook_id)
    previous = read_dashboard_entries(workbook)
    params = find_parameters(workbook)
    previous_checked = params[PARAM_CHECKED].get("value") or "an earlier date"
    previous_source = params[PARAM_SOURCE].get("value") or "the source"
    settings = reminder_settings(workbook)
    reminder_error, team, logic_app = None, [], LOGIC_APP_NAME.strip()
    if SEND_REMINDERS:
        try:
            team, logic_app = reminder_targets(workbook, team_from_settings)
        except UpdateError as error:
            reminder_error = error

    refresh_error = None
    try:
        entries = refresh_dates(arm, workbook_id, resource, workbook, previous, source, source_label, checked, dry_run)
        dates_line = "Support dates last checked %s from %s." % (display_date(checked), source_label)
    except UpdateError as error:
        refresh_error = error
        entries = previous
        dates_line = ("Support dates last checked %s from %s. This week's check of Microsoft's support dates didn't "
                      "complete, so they may be out of date." % (previous_checked, previous_source))
        log("Couldn't refresh the support dates, so the dashboard keeps its current dates (details below).")

    if not SEND_REMINDERS:
        log("Reminder emails are turned off (SEND_REMINDERS = False).")
    elif reminder_error is not None:
        log("Reminder emails weren't sent (details below).")
    elif not team:
        log("Reminder emails are off: add the database team's address to the dashboard's \"Reminder emails to\" "
            "setting and save the dashboard.")
    else:
        try:
            send_reminders(arm, workbook_id, entries, settings, team, dates_line, dry_run, logic_app)
        except UpdateError as error:
            reminder_error = error

    if refresh_error is not None and reminder_error is not None:
        raise UpdateError("Support dates: %s\nReminders: %s" % (refresh_error, reminder_error))
    if refresh_error is not None:
        raise refresh_error
    if reminder_error is not None:
        raise reminder_error
    return 0


if __name__ == "__main__":
    try:
        main()
    except UpdateError as error:
        log("ERROR: %s" % error)
        raise
