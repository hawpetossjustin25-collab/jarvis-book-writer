"""Atlanta's neighborhoods, what can be bought there, and every renovation with its price impact.

Neighborhoods and streets are real. Prices are game values scaled to each neighborhood (index 1.0 =
downtown); they are not appraisals of real parcels.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Area:
    name: str
    county: str
    index: float  # price level, downtown = 1.0
    streets: tuple[str, ...]


AREAS = (
    Area("Downtown", "Fulton", 1.00, ("Peachtree St NE", "Marietta St NW", "Pryor St SW", "Auburn Ave NE",
                                      "Mitchell St SW", "Forsyth St NW", "Broad St SW")),
    Area("Midtown", "Fulton", 1.55, ("Peachtree St NE", "W Peachtree St NW", "10th St NE", "Juniper St NE",
                                     "Piedmont Ave NE", "Crescent Ave NE")),
    Area("Buckhead", "Fulton", 2.10, ("Peachtree Rd NE", "Piedmont Rd NE", "Roswell Rd NE", "Lenox Rd NE",
                                      "W Paces Ferry Rd NW")),
    Area("Old Fourth Ward", "Fulton", 1.40, ("Edgewood Ave SE", "Boulevard NE", "Ralph McGill Blvd NE",
                                             "North Ave NE", "Glen Iris Dr NE")),
    Area("Inman Park", "Fulton", 1.60, ("Euclid Ave NE", "Elizabeth St NE", "Edgewood Ave NE", "Hurt St NE")),
    Area("Castleberry Hill", "Fulton", 1.05, ("Walker St SW", "Peters St SW", "Nelson St SW", "Larkin St SW")),
    Area("Summerhill", "Fulton", 1.10, ("Georgia Ave SE", "Hank Aaron Dr SE", "Martin St SE", "Ormond St SE")),
    Area("Grant Park", "Fulton", 1.25, ("Cherokee Ave SE", "Boulevard SE", "Augusta Ave SE", "Grant St SE")),
    Area("West End", "Fulton", 0.75, ("Ralph David Abernathy Blvd SW", "Lee St SW", "Oglethorpe Ave SW",
                                      "Lawton St SW", "Gordon St SW")),
    Area("Westview", "Fulton", 0.80, ("Westview Dr SW", "Lucile Ave SW", "Langhorn St SW", "Donnelly Ave SW")),
    Area("Adair Park", "Fulton", 0.65, ("Lexington Ave SW", "Mayland Ave SW", "Murphy Ave SW", "Allene Ave SW")),
    Area("Mechanicsville", "Fulton", 0.60, ("McDaniel St SW", "Fulton St SW", "Windsor St SW", "Ormond St SW")),
    Area("Pittsburgh", "Fulton", 0.50, ("McDaniel St SW", "Arthur St SW", "Pryor Rd SW", "Delbridge St SW")),
    Area("Vine City", "Fulton", 0.50, ("Joseph E Lowery Blvd NW", "Magnolia St NW", "Vine St NW",
                                       "Elm St NW")),
    Area("English Avenue", "Fulton", 0.45, ("Joseph E Boone Blvd NW", "Cameron St NW", "Griffin St NW",
                                            "Oliver St NW")),
    Area("Kirkwood", "DeKalb", 1.15, ("Hosea L Williams Dr NE", "Oakview Rd NE", "Howard St NE",
                                      "Warren St NE")),
    Area("East Lake", "DeKalb", 1.05, ("2nd Ave SE", "Glenwood Ave SE", "Alston Dr SE", "Memorial Dr SE")),
    Area("Edgewood", "DeKalb", 1.00, ("Hardee St NE", "Mayson Ave NE", "Arizona Ave NE", "Moreland Ave NE")),
    Area("Decatur", "DeKalb", 1.45, ("Ponce de Leon Ave", "Church St", "Commerce Dr", "Clairemont Ave")),
)
AREAS_BY_NAME = {a.name.lower(): a for a in AREAS}


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    base: float  # value in good shape at index 1.0
    rent_yield: float  # yearly rent as a share of value
    sqft: int
    commercial: bool = False


KINDS = (
    Kind("lot", "vacant land lot", 30_000, 0.0, 6_000),
    Kind("house", "single-family house", 240_000, 0.085, 1_500),
    Kind("duplex", "duplex", 330_000, 0.09, 2_400),
    Kind("condo", "condo", 260_000, 0.075, 1_000),
    Kind("townhouse", "townhouse", 310_000, 0.075, 1_800),
    Kind("apartments", "apartment building (12 units)", 1_900_000, 0.095, 10_000),
    Kind("store", "storefront store", 520_000, 0.08, 2_500, True),
    Kind("restaurant", "restaurant building", 700_000, 0.08, 3_200, True),
    Kind("storage", "self-storage facility", 3_200_000, 0.10, 60_000, True),
    Kind("office", "office building", 4_600_000, 0.075, 40_000, True),
    Kind("warehouse", "warehouse", 1_300_000, 0.085, 30_000, True),
    Kind("gas_station", "gas station", 950_000, 0.09, 2_800, True),
    Kind("strip_mall", "strip mall", 2_800_000, 0.085, 22_000, True),
    Kind("school", "private school campus", 3_800_000, 0.07, 45_000, True),
    Kind("college", "college campus", 22_000_000, 0.065, 300_000, True),
    Kind("hotel", "hotel", 9_500_000, 0.09, 80_000, True),
    Kind("car_lot", "car lot (dealership)", 2_600_000, 0.06, 4_000, True),
    Kind("mixed_use", "mixed-use development", 450_000_000, 0.07, 900_000, True),
    Kind("office_tower", "downtown office tower (skyscraper)", 320_000_000, 0.07, 1_200_000, True),
    Kind("stadium", "stadium", 1_600_000_000, 0.06, 1_800_000, True),
)
KINDS_BY_KEY = {k.key: k for k in KINDS}


@dataclass(frozen=True)
class Renovation:
    key: str
    label: str
    cost: float  # for a 1,500 sq ft house; scaled by size
    days: int
    fixes: int  # condition points it restores when that part is broken
    premium: float  # value it adds on top, as a share, when done well
    commercial_only: bool = False
    homes_only: bool = False


RENOVATIONS = (
    Renovation("roof", "new roof", 11_000, 4, 14, 0.010),
    Renovation("hvac", "new heating and air (HVAC)", 9_000, 3, 10, 0.010),
    Renovation("foundation", "foundation repair", 14_000, 10, 18, 0.0),
    Renovation("plumbing", "replumb (new pipes)", 8_500, 6, 9, 0.005),
    Renovation("electrical", "rewire and new panel", 9_500, 6, 9, 0.005),
    Renovation("windows", "new windows", 9_000, 4, 5, 0.015),
    Renovation("kitchen", "kitchen remodel", 32_000, 21, 8, 0.060, homes_only=True),
    Renovation("bathrooms", "bathroom remodel", 15_000, 14, 6, 0.035, homes_only=True),
    Renovation("flooring", "new flooring", 9_000, 5, 5, 0.020),
    Renovation("paint", "interior paint", 4_500, 4, 5, 0.015),
    Renovation("exterior", "exterior paint and siding repair", 7_000, 6, 4, 0.020),
    Renovation("curb_appeal", "landscaping and curb appeal", 3_500, 3, 2, 0.020),
    Renovation("appliances", "new appliances", 6_000, 1, 2, 0.015, homes_only=True),
    Renovation("cleanout", "trash-out and deep clean", 2_000, 2, 4, 0.0),
    Renovation("facade", "storefront facade", 25_000, 14, 4, 0.040, commercial_only=True),
    Renovation("parking", "repave parking lot", 18_000, 7, 4, 0.020, commercial_only=True),
    Renovation("sprinklers", "fire sprinklers to code", 30_000, 12, 8, 0.010, commercial_only=True),
)
RENOVATIONS_BY_KEY = {r.key: r for r in RENOVATIONS}

INDUSTRIES = {
    # monthly revenue per employee, monthly cost per employee, how much it swings, a perk in the game
    "renovation crew": (11_000, 6_500, 0.25, "your renovations cost 15% less and finish faster"),
    "property management": (6_000, 4_200, 0.15, "your rentals earn 10% more"),
    "real estate brokerage": (9_000, 5_000, 0.35, "you pay no agent commission when you sell"),
    "self storage": (7_000, 3_500, 0.10, ""),
    "restaurant": (8_500, 6_800, 0.30, ""),
    "coffee shop": (6_500, 5_200, 0.25, ""),
    "cleaning service": (5_500, 3_900, 0.20, ""),
    "trucking": (12_000, 8_800, 0.30, ""),
    "tech startup": (9_000, 9_500, 0.80, ""),
    "retail store": (7_500, 5_900, 0.25, ""),
}


def kind_for(text: str) -> Kind:
    t = (text or "").lower().strip()
    if t in KINDS_BY_KEY:
        return KINDS_BY_KEY[t]
    for k in KINDS:
        if t and (t in k.label or k.key.replace("_", " ") in t):
            return k
    aliases = {"home": "house", "apartment": "apartments", "college": "college", "university": "college",
               "storage": "storage", "shop": "store", "land": "lot", "school": "school", "mall": "strip_mall",
               "dealership": "car_lot", "car": "car_lot", "skyscraper": "office_tower", "tower": "office_tower",
               "arena": "stadium", "development": "mixed_use"}
    for word, key in aliases.items():
        if word in t:
            return KINDS_BY_KEY[key]
    raise ValueError(f"no property type like '{text}'; types: " + ", ".join(k.label for k in KINDS))


def area_for(text: str) -> Area:
    t = (text or "").lower().strip()
    if t in AREAS_BY_NAME:
        return AREAS_BY_NAME[t]
    for a in AREAS:
        if t and (t in a.name.lower() or a.name.lower() in t):
            return a
    raise ValueError(f"no Atlanta area like '{text}'; areas: " + ", ".join(a.name for a in AREAS))


def renovations_for(kind: Kind) -> list[Renovation]:
    return [r for r in RENOVATIONS if not (r.commercial_only and not kind.commercial)
            and not (r.homes_only and (kind.commercial or kind.key == "lot")) and kind.key != "lot"]


def size_factor(kind: Kind) -> float:
    return max(0.5, (kind.sqft / 1500) ** 0.85)
