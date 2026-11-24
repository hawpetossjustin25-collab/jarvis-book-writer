"""'Let's learn': how each move in the game is really done in Atlanta, with the places to go, the steps in
order, the paperwork and fees, and the law or rule behind it. Where the game simplifies, the lesson says so."""
from __future__ import annotations

PLAYBOOKS = {
    "tax_sale": {
        "title": "Buying at an Atlanta sheriff's tax sale",
        "steps": [
            "Get the list: the Fulton County Sheriff posts the tax sale list, and the ads run in the Wednesday "
            "South Fulton Neighbor for the four weeks before the sale. DeKalb's list is on the Tax "
            "Commissioner's site and in The Champion Newspaper.",
            "Research every parcel before sale day: look it up in the county property records, drive by it, and "
            "have a title search done, because the deed can come with other liens and problems.",
            "Register: DeKalb wants bidders to pre-register on its Tax Sale Pre-Registration Portal; check "
            "Fulton's sale terms on the Sheriff's Tax Sales page.",
            "Bring certified funds: the winning bid is paid on the spot (Fulton takes a cashier's check for the "
            "exact amount). A winner who doesn't pay owes the purchase money anyway.",
            "Bid on sale day, the first Tuesday of the month: bidding opens at the taxes plus costs and the "
            "highest bidder wins.",
            "Receive the sheriff's tax deed and record it. Anything bid above the taxes and costs is excess funds "
            "for the owner; the sheriff notifies them within 30 days.",
        ],
        "where": ["Fulton: the courthouse steps at 136 Pryor Street SW, Atlanta, 10 a.m. to 4 p.m. (Sheriff's "
                  "Office contact: 141 Pryor St SW, 404-612-4000)",
                  "DeKalb: the DeKalb County Courthouse steps in Decatur, 12:00 p.m.; DeKalb Tax Commissioner "
                  "404-298-4000"],
        "paperwork": ["Certified funds or cashier's check for the full bid", "Bidder registration (DeKalb)",
                      "The recorded sheriff's tax deed"],
        "law": ["O.C.G.A. 48-4-1 and following (tax sales)", "O.C.G.A. 9-13-140 to 142 (sale advertising)",
                "O.C.G.A. 9-13-170 (a winner who won't pay is liable)", "O.C.G.A. 48-4-5 (excess funds)"],
        "game_note": "In the game the list, research and registration happen for you; the bidding is the real "
                     "process.",
    },
    "redemption": {
        "title": "When the owner redeems your tax deed",
        "steps": [
            "For 12 months after the sale, the owner, a lender or anyone with an interest can redeem.",
            "They pay you what you bid, plus any taxes you paid since, plus a 20% premium for the first year or "
            "part of one, and 10% for each year after.",
            "Until then you can't collect rent or take possession.",
        ],
        "where": ["Redemption is paid to you, the tax sale purchaser (often arranged through the sheriff or tax "
                  "office that held the sale)"],
        "paperwork": ["A quitclaim deed back to the owner when they redeem"],
        "law": ["O.C.G.A. 48-4-40 (who may redeem and when)", "O.C.G.A. 48-4-42 (the redemption amount)"],
        "game_note": "In the game the owner decides on their own whether to redeem.",
    },
    "foreclose": {
        "title": "Foreclosing the right to redeem (barment)",
        "steps": [
            "Wait 12 months from the sale date.",
            "Write the notice of foreclosure of the right to redeem with a deadline date.",
            "Deliver it to the sheriff at least 45 days before that deadline; the sheriff serves the owner, the "
            "occupant and everyone with a recorded interest who lives in the county.",
            "Send it by certified or registered mail to those who live outside the county.",
            "Publish it once a week for four weeks in the county's legal newspaper within the six months before "
            "the deadline.",
            "If nobody redeems by the deadline, the right to redeem is barred and the property is yours. Most "
            "title insurers also want a quiet title action in superior court before they insure it.",
        ],
        "where": ["The county sheriff's office (service)", "The legal organ newspaper (publication)",
                  "The Clerk of Superior Court (recording, and a quiet title case if needed)"],
        "paperwork": ["Notice of foreclosure of the right to redeem", "The sheriff's entry of service",
                      "Proof of publication"],
        "law": ["O.C.G.A. 48-4-45 (who gets notice)", "O.C.G.A. 48-4-46 (service, 45 days, publication)"],
        "game_note": "In the game one action sends the notice and the deadline is 45 days later.",
    },
    "buy": {
        "title": "Buying a property in Atlanta",
        "steps": [
            "Make an offer with a written purchase and sale agreement and earnest money.",
            "Use the due diligence period for inspections and to check zoning, permits and any code violations.",
            "A real estate attorney runs the title search and the closing; in Georgia closings are handled by an "
            "attorney.",
            "At closing pay the price and closing costs; Georgia's transfer tax is $1 per $1,000 of the price "
            "(the seller usually pays it).",
            "The deed is recorded with the Clerk of Superior Court of the county (Fulton or DeKalb).",
            "If you'll live in it, file for the homestead exemption with the county Tax Assessors.",
        ],
        "where": ["A real estate attorney's office for closing", "Clerk of Superior Court, real estate records",
                  "County Board of Tax Assessors (homestead)"],
        "paperwork": ["Purchase and sale agreement", "Inspection reports", "Title commitment",
                      "Warranty or limited warranty deed", "Closing statement"],
        "law": ["O.C.G.A. 48-6-1 (transfer tax)", "O.C.G.A. 48-5-7 (property assessed at 40% of value)"],
        "game_note": "The game charges about 2% for closing costs and closes instantly.",
    },
    "renovate": {
        "title": "Renovating in the City of Atlanta",
        "steps": [
            "Get bids from licensed contractors: general, electrical, plumbing and heating-and-air contractors "
            "are licensed by Georgia boards under the Secretary of State.",
            "Pull building permits from the City of Atlanta Department of City Planning, Office of Buildings, "
            "for structural, roof, electrical, plumbing and HVAC work (paint and cleaning don't need one).",
            "In a historic district like Inman Park or Grant Park, exterior changes go through the Atlanta Urban "
            "Design Commission first.",
            "Pass the city inspections as the work goes, then close out the permits.",
        ],
        "where": ["City of Atlanta Office of Buildings (permits and inspections), 55 Trinity Ave SW",
                  "Atlanta Urban Design Commission (historic districts)"],
        "paperwork": ["Building permits", "Contractor licenses and insurance", "Inspection sign-offs"],
        "law": ["Georgia State Licensing Boards for contractors (Secretary of State)",
                "City of Atlanta building and zoning codes"],
        "game_note": "In the game work finishes on schedule and never fails an inspection.",
    },
    "sell": {
        "title": "Selling (flipping) a property",
        "steps": [
            "List it with a licensed broker (Georgia Real Estate Commission) or sell it yourself.",
            "Give buyers a seller's property disclosure.",
            "Negotiate offers and counteroffers in writing; accept one and give the buyer their due diligence "
            "period.",
            "Close at the buyer's or your attorney; pay the transfer tax, the agent's commission and any loan "
            "payoff from the proceeds.",
            "Report the profit on your taxes: a flip held a year or less is taxed as ordinary income.",
        ],
        "where": ["A real estate attorney's office for closing", "Clerk of Superior Court (the new deed)"],
        "paperwork": ["Listing agreement", "Seller's disclosure", "Purchase and sale agreement", "Deed",
                      "Closing statement"],
        "law": ["O.C.G.A. 48-6-1 (transfer tax)", "Georgia Real Estate Commission licensing"],
        "game_note": "The game uses a 5% commission when you list with an agent.",
    },
    "rent": {
        "title": "Renting out a property in Atlanta",
        "steps": [
            "Screen tenants and sign a written lease.",
            "Hold the security deposit by Georgia's rules and return it on time after move-out.",
            "Keep the property up to code; the City of Atlanta enforces housing code.",
            "An eviction (a dispossessory) goes through the county magistrate court.",
        ],
        "where": ["Fulton or DeKalb County Magistrate Court (dispossessories)"],
        "paperwork": ["Lease", "Move-in inspection", "Security deposit records"],
        "law": ["O.C.G.A. 44-7 (Georgia landlord and tenant law)"],
        "game_note": "In the game rent arrives on the 1st and tenants never miss.",
    },
    "company": {
        "title": "Starting a company in Atlanta",
        "steps": [
            "Search the name on the Georgia Secretary of State's eCorp site.",
            "File Articles of Organization (LLC) or Incorporation on eCorp: $100 plus a $10 service charge.",
            "Name a registered agent with a Georgia street address.",
            "Get a free EIN from the IRS at irs.gov.",
            "Register with the Georgia Department of Revenue (Georgia Tax Center) if you'll collect sales tax or "
            "have employees.",
            "Get the City of Atlanta Business Occupational Tax Certificate on the ATLBIZ portal, with notarized "
            "E-Verify and SAVE affidavits and a zoning check.",
            "Every year: the Georgia annual registration by April 1 ($60; $25 more if late) and the Atlanta "
            "license renewal.",
        ],
        "where": ["Georgia Secretary of State, Corporations Division (eCorp online)",
                  "City of Atlanta Office of Revenue, Atlanta City Hall, 55 Trinity Ave SW (ATLBIZ online)",
                  "IRS (EIN online)"],
        "paperwork": ["Articles of Organization or Incorporation", "EIN letter", "E-Verify affidavit",
                      "SAVE affidavit", "Occupational Tax Certificate"],
        "law": ["Georgia Secretary of State Corporations Division filing fees", "Atlanta Municipal Code 30-51 "
                "and 30-62 (occupation tax)"],
        "game_note": "The game files everything in one step and charges the real fees.",
    },
    "loan": {
        "title": "Borrowing against a property",
        "steps": [
            "Apply with a bank or commercial lender; they order an appraisal and a title search.",
            "In Georgia the loan is secured with a security deed (deed to secure debt), not a mortgage.",
            "Pay Georgia's intangible recording tax, $1.50 per $500 of the loan, when the security deed is "
            "recorded.",
            "The security deed is recorded with the Clerk of Superior Court and paid off at sale.",
        ],
        "where": ["Your lender", "A closing attorney", "Clerk of Superior Court"],
        "paperwork": ["Loan application", "Appraisal", "Promissory note", "Security deed"],
        "law": ["O.C.G.A. 48-6-61 (intangible recording tax)"],
        "game_note": "The game lends 75% of value at 8% with no fees or approval.",
    },
    "car_lot": {
        "title": "Running a car lot in Georgia",
        "steps": [
            "Form your company and get the City of Atlanta business license.",
            "Get a used car dealer license from the Georgia State Board of Registration of Used Motor Vehicle "
            "Dealers and Used Motor Vehicle Parts Dealers (Secretary of State); new cars need a manufacturer "
            "franchise.",
            "Check zoning for a car lot at the address.",
            "Handle titles and tags for buyers through the county Tax Commissioner's tag office.",
        ],
        "where": ["Georgia Secretary of State licensing boards", "County Tax Commissioner (titles and tags)"],
        "paperwork": ["Dealer license", "Surety bond", "Business license", "Title paperwork"],
        "law": ["Georgia used motor vehicle dealer licensing law"],
        "game_note": "In the game cars sell on their own over the months.",
    },
    "transit": {
        "title": "Atlanta's transit in real life",
        "steps": [
            "MARTA is a public authority created by Georgia law in 1965 and run by a board from the City of "
            "Atlanta and the counties it serves; it can't be bought.",
            "The Atlanta Streetcar belongs to the City of Atlanta.",
            "Private companies take part through public-private partnerships: contracts to build or operate "
            "lines.",
            "A hyperloop would need state (GDOT) and federal transportation approvals.",
        ],
        "where": ["MARTA headquarters", "City of Atlanta", "Georgia Department of Transportation"],
        "paperwork": ["Public-private partnership agreements and federal approvals"],
        "law": ["The Metropolitan Atlanta Rapid Transit Authority Act of 1965"],
        "game_note": "Buying them is the game's own twist on reality.",
    },
    "trading": {
        "title": "Day trading Micro Nasdaq futures for real",
        "steps": [
            "Open a futures account with a broker registered with the CFTC and the NFA.",
            "MNQ is the CME Micro E-mini Nasdaq-100: $2 per index point, ticks of 0.25 ($0.50).",
            "Brokers set day-trading margin per contract; losses can exceed what you put in.",
            "The pattern day trader rule is for stocks, not futures.",
            "Futures profits are Section 1256 contracts: 60% long-term and 40% short-term for taxes.",
        ],
        "where": ["A CFTC/NFA-registered futures broker"],
        "paperwork": ["Futures account application", "Risk disclosure statements"],
        "law": ["Commodity Exchange Act (CFTC)", "Internal Revenue Code section 1256"],
        "game_note": "The game's market is simulated; real trading can lose real money.",
    },
    "bidder": {
        "title": "Sending someone to bid for you",
        "steps": [
            "Decide the name the deed should be in (often your company).",
            "Give your bidder written authorization and the certified funds.",
            "Register them as required by that county's sale terms.",
            "After the sale, check that the deed was issued and recorded in the right name.",
        ],
        "where": ["The county's tax sale (Fulton: 136 Pryor St SW; DeKalb: Decatur courthouse)"],
        "paperwork": ["Written authorization", "Bidder registration", "Certified funds"],
        "law": ["Each county's conditions of sale"],
        "game_note": "In the game your assistant always shows up and follows your limits.",
    },
    "title": {
        "title": "Clearing old loans and liens off a tax deed property's title",
        "steps": [
            "Before the sale, order a title search: look up the parcel in the county deed records and on GSCCCA.org "
            "for every security deed (Georgia's mortgage), judgment, City lien and federal tax lien.",
            "When you send the notice to foreclose the right of redemption, serve it on everyone with a recorded "
            "interest: the owner, every lender holding a security deed and every judgment creditor. Any of them "
            "can redeem; those properly served who don't redeem by the deadline lose their claim.",
            "If the IRS has a federal tax lien, it has 120 days after the sale to redeem.",
            "Liens that survive (often City of Atlanta code-enforcement or water liens): get a written payoff "
            "letter, pay it, and have the release or cancellation recorded with the Clerk of Superior Court. A "
            "lender must cancel a paid-off security deed within 60 days.",
            "File a quiet title petition in the county's Superior Court. The judge appoints a special master, "
            "everyone with a possible claim gets notice, and the court issues a decree that the title is yours.",
            "Record the decree with the Clerk of Superior Court and buy an owner's title insurance policy. Now a "
            "bank will lend on it and a buyer can get insured title.",
        ],
        "where": ["Fulton County Clerk of Superior Court (real estate records), Fulton County Courthouse, "
                  "136 Pryor Street SW, Atlanta",
                  "DeKalb County Clerk of Superior Court, DeKalb County Courthouse, 556 N. McDonough Street, "
                  "Decatur",
                  "GSCCCA.org: Georgia's statewide online deed and lien index",
                  "A Georgia real estate attorney (files the quiet title case) and a title insurance company"],
        "paperwork": ["Title search report", "Barment notice with proof of service and publication",
                      "Payoff letters and recorded lien releases", "Quiet title petition, special master's report "
                      "and final decree", "Owner's title insurance policy",
                      "Georgia's recording fee: $25 for each release or decree recorded"],
        "law": ["O.C.G.A. 48-4-45 and 48-4-46 (who must get the barment notice)",
                "O.C.G.A. 23-3-60 to 23-3-72 (Quiet Title Act, quiet title in rem)",
                "O.C.G.A. 44-14-3 (cancelling a paid security deed)",
                "26 U.S.C. 7425(d) (the IRS's 120 days to redeem)",
                "O.C.G.A. 15-6-77 (recording fees)"],
        "game_note": "The game does the title search for you, treats a served notice as wiping out the old "
                     "security deeds and judgments, makes you pay off City liens, and finishes quiet title in "
                     "90 days for $4,500 plus the liens. Real cases take longer and cost more, so use an attorney.",
    },
    "personal_loan": {
        "title": "Taking a loan to buy more tax deeds",
        "steps": [
            "Check your credit report (annualcreditreport.com) and score; lenders look at the score and your "
            "debt-to-income ratio.",
            "Compare lenders (banks, Atlanta credit unions, online lenders); prequalify with a soft credit check.",
            "Apply with ID, proof of income (tax returns if self-employed) and bank statements; the lender then "
            "does a hard credit check.",
            "Read the Truth in Lending disclosure before you sign: the APR, monthly payment, origination fee and "
            "any rule against using the money for business or investing.",
            "To borrow against the property instead (cash-out refinance, home equity line or investor loan): the "
            "title must be clear and insurable, the lender orders an appraisal, and a security deed is recorded "
            "with Georgia's intangible recording tax of $1.50 per $500.",
            "For the tax sale: turn the loan money into certified funds or cashier's checks before sale day, "
            "because the winning bid is paid on the spot.",
        ],
        "where": ["Your bank or credit union", "A closing attorney (for a loan secured by the property)",
                  "The sheriff's tax sale with certified funds"],
        "paperwork": ["Loan application, ID, income proof and bank statements", "Truth in Lending disclosure "
                      "and promissory note", "Security deed (only if the loan is secured by the property)"],
        "law": ["Truth in Lending Act, 15 U.S.C. 1601 and Regulation Z (loan disclosures)",
                "Equal Credit Opportunity Act, 15 U.S.C. 1691", "O.C.G.A. 48-6-61 (intangible recording tax)"],
        "game_note": "In the game a personal loan is approved up to 30% of your clear-title equity ($100,000 at "
                     "most) at 12% over 60 months, with no credit check; payments come out on the 1st.",
    },
}
ALIASES = {"bid": "tax_sale", "auction": "tax_sale", "tax": "tax_sale", "deed": "tax_sale", "redeem": "redemption",
           "foreclos": "foreclose", "barment": "foreclose", "buy": "buy", "purchase": "buy", "offer": "buy",
           "renovat": "renovate", "repair": "renovate", "fix": "renovate", "permit": "renovate", "sell": "sell",
           "flip": "sell", "list": "sell", "rent": "rent", "tenant": "rent", "lease": "rent", "compan": "company",
           "llc": "company", "business": "company", "license": "company", "start": "company", "loan": "loan",
           "borrow": "loan", "car": "car_lot", "dealer": "car_lot", "marta": "transit", "bus": "transit",
           "rail": "transit", "streetcar": "transit", "hyperloop": "transit", "trad": "trading",
           "nasdaq": "trading", "futures": "trading", "assistant": "bidder", "bidder": "bidder", "mortgage": "title", "security deed": "title",
           "quiet title": "title", "title": "title", "personal loan": "personal_loan"}


def playbook_for(topic: str) -> str:
    t = (topic or "").lower()
    if t in PLAYBOOKS:
        return t
    for word in sorted(ALIASES, key=len, reverse=True):  # the most specific word wins: 'foreclos' over 'deed'
        if word in t:
            return ALIASES[word]
    return ""
