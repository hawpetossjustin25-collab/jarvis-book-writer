"""The real Atlanta rules the game follows, each with where it comes from.

Numbers marked "game estimate" are not set by law (they vary deal by deal); everything else is from the
cited statute or office and is what a buyer in Atlanta actually faces.
"""
from __future__ import annotations

from datetime import date, timedelta

# ---- tax sales (Georgia sells tax deeds that the owner can redeem, not lien certificates) -------------
REDEMPTION_PREMIUM_FIRST_YEAR = 0.20  # O.C.G.A. 48-4-42(a)(3)
REDEMPTION_PREMIUM_LATER_YEARS = 0.10  # each further year or fraction of a year, same section
REDEMPTION_WAIT_DAYS = 365  # O.C.G.A. 48-4-45(a): barring the right to redeem may start 12 months after the sale
NOTICE_LEAD_DAYS = 45  # O.C.G.A. 48-4-46(b): notice goes to the sheriff at least 45 days before the deadline
NOTICE_COST = 450.0  # game estimate: sheriff's service plus four weeks of newspaper publication
EXCESS_FUNDS_YEARS = 5  # O.C.G.A. 48-4-5(c): unclaimed excess goes to the state after five years
SALE_PLACES = {
    "Fulton": "the courthouse steps at 136 Pryor Street SW, downtown Atlanta, between 10 a.m. and 4 p.m.",
    "DeKalb": "the DeKalb County Courthouse steps in Decatur, at 12:00 p.m.",
}
SALE_PAPERS = {"Fulton": "South Fulton Neighbor (Wednesday edition, four weeks before)",
               "DeKalb": "The Champion Newspaper (once a week for four weeks before)"}
TAX_SALE_COSTS = 285.0  # game estimate of the levy, recording, advertising and commission costs added to the taxes

# ---- property ---------------------------------------------------------------------------------------
ASSESSMENT_RATIO = 0.40  # Georgia assesses property at 40% of fair market value (O.C.G.A. 48-5-7)
MILLAGE = 0.040  # game estimate of a typical Atlanta (Fulton/DeKalb + city + schools) rate per $1 assessed
TRANSFER_TAX_PER_1000 = 1.0  # Georgia real estate transfer tax: $1 per $1,000 of price (O.C.G.A. 48-6-1)
BUYER_CLOSING_RATE = 0.02  # game estimate: attorney closing, title insurance, recording
AGENT_COMMISSION = 0.05  # game estimate: listing with an agent
TAX_BILL_DAY = (11, 15)  # game: yearly property taxes are paid on November 15

# ---- starting a company in Atlanta ------------------------------------------------------------------
SOS_ARTICLES = 110.0  # Articles of Organization (LLC) / Incorporation: $100 + $10 service charge, Georgia SOS
NAME_RESERVATION = 35.0  # 30-day name reservation, Georgia SOS
ANNUAL_REGISTRATION = 60.0  # due April 1 every year (Jan 1 earliest), Georgia SOS
LATE_REGISTRATION_PENALTY = 25.0
OTC_FLAT = 50.0  # City of Atlanta occupation tax: flat $50 on the first $10,000 of gross receipts
OTC_PER_EMPLOYEE = 25.0  # $25 per employee, the first employee is exempt
OTC_RATE_ESTIMATE = 0.0015  # game estimate of the business-class rate on receipts above $10,000
ENTITY_TYPES = {"llc": "Limited Liability Company", "corporation": "Corporation"}

SOURCES = {
    "redemption": "O.C.G.A. 48-4-40 and 48-4-42; DeKalb County Tax Commissioner, Tax Sales",
    "foreclosure": "O.C.G.A. 48-4-45 and 48-4-46",
    "excess": "O.C.G.A. 48-4-5",
    "tax_sale": "Fulton County Sheriff's Office, Tax Sales; DeKalb County Tax Commissioner, Tax Sales",
    "company": "Georgia Secretary of State, Corporations Division Filing Fees (effective Sept 6, 2025)",
    "license": "City of Atlanta Office of Revenue, Business Occupational Tax Certificate (ATLBIZ portal)",
    "transfer_tax": "O.C.G.A. 48-6-1",
}

LESSONS = {
    "tax sale": (
        "Georgia doesn't sell tax lien certificates; it sells tax deeds the owner can still redeem. When taxes "
        "go unpaid, the Tax Commissioner issues an execution (a fi. fa.) and the sheriff sells the property on "
        "the first Tuesday of the month. Fulton sells on the courthouse steps at 136 Pryor Street SW between "
        "10 and 4; DeKalb sells at noon in Decatur. Bidding opens at the taxes plus costs, and the highest "
        "bidder pays right then. Anything bid above the taxes and costs is excess funds, held for the owner.",
        "tax_sale"),
    "redemption": (
        "For 12 months after the sale the owner, or anyone with an interest like a lender, can redeem by "
        "paying you what you bid, plus any taxes you paid since, plus a 20% premium for the first year (or "
        "part of one) and 10% for each year after. You can't collect rent or move in while it can be "
        "redeemed.", "redemption"),
    "foreclosure": (
        "After 12 months you can bar the right to redeem: you send a notice of foreclosure through the "
        "sheriff to the owner, the occupant and everyone with a recorded interest, at least 45 days before "
        "the deadline in the notice, and publish it once a week for four weeks. If nobody redeems by the "
        "deadline, the property is yours outright.", "foreclosure"),
    "excess funds": (
        "When a property sells at a tax sale for more than the taxes and costs, the extra is excess funds. "
        "The sheriff must tell the owner within 30 days; unclaimed funds go to the state after five years.",
        "excess"),
    "company": (
        "In Atlanta you start a company in three steps: file Articles of Organization (LLC) or Incorporation "
        "with the Georgia Secretary of State on eCorp ($100 plus a $10 service charge, so $110), name a "
        "registered agent with a Georgia street address, then get a City of Atlanta Business Occupational "
        "Tax Certificate on the ATLBIZ portal, with notarized E-Verify and SAVE affidavits. Every year by "
        "April 1 you file the state annual registration ($60, or $25 more if late).", "company"),
    "business license": (
        "Atlanta's occupation tax is a flat $50 on the first $10,000 of gross receipts, then a rate set by "
        "your business class, plus $25 for each employee after the first. It is renewed every year.",
        "license"),
    "buying": (
        "Buying a house in Georgia closes at a real estate attorney's office. Georgia's transfer tax is $1 "
        "per $1,000 of the price, and property is assessed at 40% of its value for property taxes.",
        "transfer_tax"),
}


def first_tuesday(year: int, month: int) -> date:
    """The sheriff's sale day: the first Tuesday, or the next business day when that's a legal holiday."""
    d = date(year, month, 1)
    d += timedelta(days=(1 - d.weekday()) % 7)
    while (d.month, d.day) in ((1, 1), (7, 4)) or d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def next_sale_day(today: date) -> date:
    d = first_tuesday(today.year, today.month)
    if d >= today:
        return d
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return first_tuesday(year, month)


def redemption_amount(paid: float, taxes_paid_since: float, sale_day: date, on: date) -> float:
    """What the owner must pay to redeem on a given day (O.C.G.A. 48-4-42(a))."""
    base = paid + taxes_paid_since
    days = max(0, (on - sale_day).days)
    years = max(1, -(-days // 365))  # the first year or any fraction of it counts as a year
    premium = REDEMPTION_PREMIUM_FIRST_YEAR + REDEMPTION_PREMIUM_LATER_YEARS * (years - 1)
    return round(base * (1 + premium), 2)


def occupation_tax(gross: float, employees: int) -> float:
    above = max(0.0, gross - 10_000)
    return round(OTC_FLAT + above * OTC_RATE_ESTIMATE + OTC_PER_EMPLOYEE * max(0, employees - 1), 2)


def yearly_property_tax(value: float) -> float:
    return round(value * ASSESSMENT_RATIO * MILLAGE, 2)
