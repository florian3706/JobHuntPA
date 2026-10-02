#!/usr/bin/env python3
"""Fill a NEW database with a fictional "Dummy workspace" for README screenshots.

    python3 tools/demo_data.py PATH/TO/new-demo.db        (or set JOBHUNT_DEMO_DB)

Everything here is made up: the person (Alex Sample), the companies, the job ads, the fit
scores, the company research, the cover letter and the chat. URLs use the reserved ``.example``
domain, and the pins are generic points in Sydney. Nothing is read from, or written to, a
real JobHuntPA database.

Safety:
- Refuses to run when the target is (or sits inside) the project's ``data/`` folder, so
  ``data/jobhunt.db`` can never be touched, and refuses to overwrite an existing file
  unless ``--overwrite`` is given.
- Never reads ``.env``: ``backend.config`` is replaced by a stub before the app code loads.
- Never touches the network: coordinates are written directly, and the geocoder cache is
  pre-filled for the few addresses the job page would otherwise look up.
- Documents are text-only rows (``stored_name`` is empty), so ``data/uploads`` is not used.

The database is built with the app's own models and helpers (``backend.db``,
``backend.filters``, ``backend.scorer.build_profile``, ``backend.duplicates.scan``), so fit
scores are not "stale", exclusion reasons are the ones the app would give, and the merged and
suggested duplicates come from the app's own duplicate scan.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WORKSPACE_NAME = "Dummy workspace"


# --------------------------------------------------------------------------
# Safety checks, before any app code is imported
# --------------------------------------------------------------------------

def target_path(arg: str, overwrite: bool) -> Path:
    path = Path(arg).expanduser().resolve()
    real = (DATA_DIR / "jobhunt.db").resolve()
    inside_data = path == DATA_DIR.resolve() or DATA_DIR.resolve() in path.parents
    same_file = path.exists() and real.exists() and os.path.samefile(path, real)
    if path == real or inside_data or same_file:
        sys.exit(f"Refusing to use {path}: it is the real data folder or database. "
                 "Give a new file somewhere else (for example in a temporary folder).")
    if path.is_dir():
        sys.exit(f"{path} is a folder; give a file name for the new database.")
    if path.exists():
        if not overwrite:
            sys.exit(f"{path} already exists. Use a new file name, or --overwrite to replace it.")
        for suffix in ("", "-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def isolate(db_path: Path) -> None:
    """Point the app at ``db_path`` and keep the real .env out of this process."""
    os.environ["JOBHUNT_DB_PATH"] = str(db_path)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["JOBHUNT_SCHEDULER"] = "off"
    sys.path.insert(0, str(ROOT))
    import backend  # noqa: F401  (the package only; backend.config is replaced below)

    stub = types.ModuleType("backend.config")
    stub.PROJECT_ROOT = ROOT
    stub.load_dotenv = lambda *args, **kwargs: None
    stub.USER_AGENT = "JobHuntPA demo seeding (offline)"
    stub.ROBOTS_AGENT = "jobhuntpa"
    sys.modules["backend.config"] = stub
    backend.config = stub


# --------------------------------------------------------------------------
# The fictional person and their documents
# --------------------------------------------------------------------------

CV_FILENAME = "Alex_Sample_CV.pdf"
CV_TEXT = """ALEX SAMPLE
Product Manager | Strathfield NSW | alex.sample@example.com | 0491 570 156

SUMMARY
Product manager with eight years in B2B software, the last six in SaaS. I run discovery with customers, turn it into a roadmap engineers believe in, and check the result with data. Strongest in platforms, payments and workflow products.

EXPERIENCE
Senior Product Manager, Brightfield Logistics Software, Sydney (Mar 2022 - present)
- Own the roadmap for the freight-visibility platform used by 90 logistics customers: three squads, 24 engineers
- Led the pricing and packaging redesign that lifted average revenue per account by 12%
- Shipped a public reporting API with data engineering; 35% of enterprise customers now use it
- Launched the driver mobile app (iOS and Android) after 40+ driver interviews and monthly usability tests
- Mentored two associate product managers; ran quarterly OKRs with the leadership team

Product Manager, Tidewater Payments, Sydney (Feb 2019 - Mar 2022)
- Launched merchant onboarding and payouts; onboarding conversion up 18% in two quarters
- Cut time to first payment from 21 days to 9 after merchant interviews and funnel analysis
- Worked with Risk and Compliance on KYC and AML requirements
- Ran 30+ A/B tests; wrote SQL daily for opportunity sizing and experiment analysis

Business Analyst, Marlin Insurance Group, Sydney (Jan 2016 - Feb 2019)
- Mapped claims and policy-change workflows for the operations team
- Wrote the requirements for two releases of the customer self-service portal

EDUCATION AND CERTIFICATES
Bachelor of Commerce (Information Systems), Eastgate University
Certified Scrum Product Owner (CSPO)

TOOLS
SQL, Amplitude, Looker, Jira, Productboard, Figma, Miro
"""

OLD_LETTER_FILENAME = "Cover_letter_Tidewater_Payments.docx"
OLD_LETTER_TEXT = """Dear Hiring Manager,

Merchants do not want to learn about payments; they want to get paid. That idea has shaped my product work at Brightfield Logistics Software, where I own a platform roadmap across three squads, and it is why the Product Manager role at Tidewater Payments caught my attention.

At Marlin Insurance Group I mapped claims workflows and wrote requirements for a customer portal, which taught me how much of a good product is the unglamorous work behind the screen. I have since moved from analysis into product ownership, running discovery with customers, working with engineers and checking results against the numbers.

I would welcome the chance to talk about how that approach could help your merchants.

Kind regards,
Alex Sample
"""

# What the dummy CV evidences, quoted back in the fit results.
EVIDENCE = {
    "years": ("Eight years in product and analysis roles, from Business Analyst to Senior Product Manager",
              ["Brightfield Logistics Software, Senior Product Manager, 2022 to present",
               "Tidewater Payments, Product Manager, 2019 to 2022", "Marlin Insurance Group, Business Analyst, 2016 to 2019"]),
    "b2b": ("Six years of B2B SaaS product work in logistics and payments",
            ["Brightfield Logistics Software: platform used by 90 customers", "Tidewater Payments: merchant onboarding and payouts"]),
    "roadmap": ("Owns the roadmap for a freight-visibility platform across three squads and 24 engineers",
                ["Brightfield Logistics Software, 2022 to present", "Prioritises with quarterly OKRs agreed with the leadership team"]),
    "sql": ("Writes SQL daily to size opportunities and analyse experiments",
            ["Tidewater Payments: 30+ A/B tests analysed", "Uses Amplitude and Looker for product metrics"]),
    "discovery": ("Runs continuous discovery: 40+ driver interviews and monthly usability tests",
                  ["Brightfield: driver mobile app", "Tidewater: merchant interviews cut time to first payment from 21 to 9 days"]),
    "stakeholders": ("Manages stakeholders across engineering, sales, finance, risk and the leadership team",
                     ["Quarterly OKR reviews with the leadership team", "Worked with Risk and Compliance at Tidewater Payments"]),
    "pricing": ("Led the pricing and packaging redesign that lifted average revenue per account by 12%",
                ["Brightfield Logistics Software, with Finance and Sales"]),
    "api": ("Shipped a public reporting API with data engineering; 35% of enterprise customers use it",
            ["Brightfield Logistics Software"]),
    "mentoring": ("Mentored two associate product managers", ["Brightfield Logistics Software"]),
    "payments": ("Launched merchant onboarding and payouts at Tidewater Payments",
                 ["Onboarding conversion up 18% in two quarters", "Time to first payment cut from 21 days to 9"]),
    "funnel": ("Improved the merchant onboarding funnel with analysis and A/B tests",
               ["Onboarding conversion up 18% in two quarters", "30+ A/B tests run at Tidewater Payments"]),
    "kyc": ("Worked with Risk and Compliance on KYC and AML requirements", ["Tidewater Payments, 2019 to 2022"]),
    "mobile": ("Launched the driver mobile app (iOS and Android)", ["Brightfield Logistics Software", "40+ driver interviews beforehand"]),
    "insurance": ("Three years at an insurer mapping claims and policy workflows",
                  ["Marlin Insurance Group, Business Analyst, 2016 to 2019"]),
    "portal": ("Wrote the requirements for two releases of a customer self-service portal", ["Marlin Insurance Group"]),
    "agile": ("Certified Scrum Product Owner; runs backlog refinement and sprint reviews for three squads",
              ["CSPO certificate", "Brightfield Logistics Software"]),
    "figma": ("Works with designers in Figma and Miro on flows and prototypes", ["Tools listed in the CV"]),
    "experiments": ("Ran 30+ A/B tests with SQL-based analysis", ["Tidewater Payments, 2019 to 2022"]),
    "strategy": ("Sets quarterly OKRs and presents the roadmap to the leadership team",
                 ["Brightfield Logistics Software, 2022 to present"]),
    "remote": ("Works daily with colleagues across Sydney and a Manila support team", ["Brightfield Logistics Software"]),
}


# --------------------------------------------------------------------------
# Places (generic points in Sydney) and pins
# --------------------------------------------------------------------------

HOME = (-33.873, 151.094)           # Strathfield
CBD = (-33.8688, 151.2093)
PARRAMATTA = (-33.8150, 151.0011)
CITY_CENTRE = (-33.8698, 151.2083)  # where an ad that only says "Sydney NSW" lands
PYRMONT = (-33.8697, 151.1955)
SURRY_HILLS = (-33.8856, 151.2131)
NORTH_SYDNEY = (-33.8404, 151.2073)
ULTIMO = (-33.8800, 151.1982)
CHATSWOOD = (-33.7960, 151.1830)
ALEXANDRIA = (-33.9100, 151.1950)
MACQUARIE_PARK = (-33.7770, 151.1230)
RHODES = (-33.8300, 151.0870)
BONDI_JUNCTION = (-33.8915, 151.2480)
BARANGAROO = (-33.8627, 151.2022)
WOLLONGONG = (-34.4250, 150.8930)

PINS = [
    ("Home (Strathfield)", "home", HOME, 13.0),
    ("CBD (hybrid)", "hybrid", CBD, 8.0),
    ("Parramatta (onsite)", "onsite", PARRAMATTA, 10.0),
]

SOURCES = {  # key: (label, careers url, method)
    "wattle": ("Wattle Analytics", "https://careers.wattle-analytics.example/jobs", "Greenhouse job board API (wattle-analytics)"),
    "saltbush": ("Saltbush Payments", "https://jobs.saltbush-payments.example/saltbush", "Lever postings API (jobs.saltbush-payments.example)"),
    "copperleaf": ("Copperleaf Health", "https://copperleaf-health.example/careers", "Ashby job board API (copperleaf-health)"),
    "kestrel": ("Kestrel Mobility", "https://careers.kestrel-mobility.example/roles", "HTML careers page (careers.kestrel-mobility.example)"),
    "ironbark": ("Ironbark Energy", "https://www.ironbark-energy.example/about/careers", "HTML careers page (ironbark-energy.example)"),
    "gumnut": ("Gumnut Software", "https://gumnut-software.example/careers", "Greenhouse job board API (gumnut-software)"),
}


# --------------------------------------------------------------------------
# The job ads
# --------------------------------------------------------------------------
# status: to_review | shortlisted | applied | interviewing | rejected | not_interested
# fit: (score, [(requirement, must_have, evidence key or None)], [gaps], summary)

def seek(n: int) -> str:
    return f"https://www.seek.example/job/{n}"


JOBS = [
    dict(
        key="wattle", company="Wattle Analytics", title="Senior Product Manager, Data Platform",
        location="Pyrmont, Sydney NSW", mode="hybrid", days=2, salary="$165,000 – $185,000 + super",
        source="greenhouse:wattle-analytics", src="wattle", url="https://careers.wattle-analytics.example/jobs/4412873",
        age=(11, 3), detail="full", place=PYRMONT, status="interviewing",
        office=("Level 4, 12 Edward Street, Pyrmont NSW 2009", "ad", PYRMONT),
        desc="""About Wattle Analytics
Wattle Analytics builds the data platform that mid-sized Australian retailers, utilities and councils use to turn raw operational data into dashboards and decisions. We are 140 people, Series B funded, and based in Pyrmont.

The role
As Senior Product Manager, Data Platform you will own the roadmap for our ingestion, modelling and self-serve reporting layers, working with three engineering squads and a design lead. You will:
- Set the vision and quarterly priorities for the Data Platform area, using customer research and usage data
- Run continuous discovery with the analysts and data teams who use Wattle every day
- Partner with Sales and Customer Success to turn feedback into scoped, measurable bets
- Define success metrics and report progress to the leadership team each quarter
- Shape pricing and packaging for platform features with Finance

About you
- 5+ years in product management, at least 3 of them in B2B SaaS
- Comfortable with data: you write SQL to answer your own questions
- Experience with APIs or developer-facing products is a plus
- Clear written communication and strong stakeholder management
- Experience mentoring other product managers is a plus

What we offer
- Hybrid: two days a week in our Pyrmont office, the rest from home
- $165,000 – $185,000 plus super, depending on experience
- Four weeks' leave, a wellbeing day each quarter and a learning budget""",
        fit=(92, [
            ("5+ years in product management", True, "years"),
            ("3+ years in B2B SaaS", True, "b2b"),
            ("Owns a platform or data product roadmap", True, "roadmap"),
            ("Writes SQL to answer own questions", True, "sql"),
            ("Continuous discovery with customers", True, "discovery"),
            ("Strong stakeholder management", True, "stakeholders"),
            ("Pricing and packaging input", False, "pricing"),
            ("API or developer-facing product experience", False, "api"),
            ("Mentoring other product managers", False, "mentoring"),
        ], ["Customer domains (retail, utilities, councils) are new to the candidate",
            "No evidence of owning a data-ingestion or modelling layer specifically"],
            "Very strong fit. Alex owns a three-squad platform roadmap, writes SQL daily and shipped a reporting API with data engineering, which maps directly onto the Data Platform brief. The main gaps are retail and utility customer domains and no direct ownership of an ingestion layer."),
    ),
    dict(
        key="wattle_seek", company="Wattle Analytics", title="Senior Product Manager - Data Platform",
        location="Pyrmont, Sydney NSW", mode="hybrid", days=None, salary=None, source="seek",
        url=seek(81240117), age=(2, 0), run_seen=True, detail="summary", place=PYRMONT, status="to_review",
        industry="Information & Communication Technology",
        desc="""Own the roadmap for the ingestion, modelling and reporting layers of Wattle's data platform. Hybrid in our Pyrmont office. 5+ years in product management, B2B SaaS and SQL skills required. Series B scale-up of 140 people.""",
        fit=None,
    ),
    dict(
        key="saltbush", company="Saltbush Payments", title="Product Manager, Merchant Onboarding",
        location="Sydney NSW", mode="hybrid", days=3, salary="$140,000 – $160,000 + super",
        source="lever:jobs.saltbush-payments.example", src="saltbush", url="https://jobs.saltbush-payments.example/saltbush/b3d1f6a0",
        age=(8, 5), detail="full", place=SURRY_HILLS, status="applied",
        office=("Level 2, 88 Foveaux Street, Surry Hills NSW 2010", "company", SURRY_HILLS),
        desc="""Saltbush Payments helps small and medium merchants take payments, get paid faster and understand their cash flow. Our platform processes more than $4 billion a year for 28,000 Australian businesses.

The role
You will own merchant onboarding: the path from sign-up to first payment. It is a high-leverage area where small improvements move revenue and risk at the same time.
- Own the onboarding roadmap and the KPIs for activation, approval rate and time to first transaction
- Work with Risk and Compliance on KYC and AML requirements without adding avoidable friction
- Run experiments and usability tests with new merchants
- Work with engineering, design and data on a squad of eight

You bring
- 3+ years as a product manager in payments, fintech or another regulated environment
- Strong analytical skills (SQL, Amplitude or similar)
- A record of improving conversion funnels
- Confidence working with compliance, legal and risk partners
- Experience with card-present products or payment terminals is a plus

Working with us
We work hybrid, three days a week in our office, with flexible hours. $140,000 – $160,000 plus super.""",
        fit=(88, [
            ("3+ years as a product manager in payments or a regulated environment", True, "payments"),
            ("Strong analytical skills (SQL, Amplitude)", True, "sql"),
            ("Record of improving conversion funnels", True, "funnel"),
            ("Works with compliance, legal and risk partners", True, "kyc"),
            ("Runs experiments and usability tests with users", True, "discovery"),
            ("Leads a squad of eight", False, "roadmap"),
            ("Card-present products or payment terminals", False, None),
        ], ["No experience with card-present products or payment terminals"],
            "Strong fit for a payments onboarding role: Alex launched merchant onboarding and payouts at Tidewater Payments, lifted conversion 18% and worked with Risk and Compliance on KYC and AML. The only clear gap is hardware and card-present experience, which the ad lists as a plus."),
    ),
    dict(
        key="harbourline", company="Harbourline Insurance", title="Senior Product Manager, Claims Experience",
        location="North Sydney NSW", mode="hybrid", days=None, salary="$155,000 – $175,000 + super", source="seek",
        url=seek(81233904), age=(5, 2), detail="summary", place=NORTH_SYDNEY, status="shortlisted",
        industry="Insurance & Superannuation",
        desc="""Lead digital claims for one of Australia's growing general insurers. Own the roadmap for the customer claims app and portal, working with Claims Operations and Technology. Insurance or regulated-industry experience is valued. Hybrid, North Sydney.""",
        fit=(84, [
            ("Product ownership of a digital claims app or customer portal", True, "portal"),
            ("Insurance or regulated-industry experience", True, "insurance"),
            ("Roadmap ownership", True, "roadmap"),
            ("Works with Operations and Technology teams", True, "stakeholders"),
        ], ["Only the listing summary was available: seniority, team size and the claims systems involved could not be checked",
            "No direct claims-platform product ownership"],
            "Good fit on the listing summary: an insurance background at Marlin Insurance Group plus a platform roadmap and portal requirements. Only the short summary was available, so seniority, team size and salary band could not be checked."),
    ),
    dict(
        key="copperleaf", company="Copperleaf Health", title="Product Manager, Clinician Platform",
        location="Remote - Australia", mode="remote", days=None, salary="$145,000 – $165,000 + super",
        source="ashby:copperleaf-health", src="copperleaf", url="https://copperleaf-health.example/careers/clinician-platform-pm",
        age=(0, 0), run_seen=True, detail="full", place=None, status="to_review",
        desc="""Copperleaf Health makes scheduling and care-coordination software used by 1,200 allied-health and community-care providers across Australia and New Zealand.

The role
You will lead the Clinician Platform: the web and mobile tools clinicians use every day to manage clients, notes and referrals. You will work with a squad of seven engineers, a designer and a clinical advisor.
- Own the roadmap and release plan for the clinician web and mobile apps
- Run research with clinicians and practice managers, in clinics and by video
- Define metrics for adoption, time saved per appointment and retention
- Work with Security and Legal on privacy requirements for health information

About you
- 4+ years in product management on a B2B SaaS product
- Experience shipping both web and mobile products
- Healthcare, allied-health or community-care experience (essential)
- Clear communicator who is comfortable working remotely

Fully remote within Australia, with a team get-together in Sydney or Melbourne each quarter.""",
        fit=(81, [
            ("4+ years in product management on B2B SaaS", True, "b2b"),
            ("Shipped web and mobile products", True, "mobile"),
            ("Research with end users (clinicians)", True, "discovery"),
            ("Healthcare or allied-health domain experience", True, None),
            ("Privacy and compliance requirements", False, "kyc"),
            ("Comfortable working remotely", False, "remote"),
        ], ["No healthcare or allied-health experience, which the ad lists as essential",
            "No evidence of handling health-information privacy rules"],
            "Solid match for a B2B SaaS role with a web and mobile client, and Alex has run research with end users. The missing piece is the healthcare domain, which the ad treats as essential."),
    ),
    dict(
        key="kestrel", company="Kestrel Mobility", title="Group Product Manager",
        location="Sydney CBD, NSW", mode="hybrid", days=3, salary="$180,000 – $200,000 + super",
        source="site:careers.kestrel-mobility.example", src="kestrel", url="https://careers.kestrel-mobility.example/roles/group-product-manager",
        age=(6, 8), detail="full", place=CBD, status="shortlisted",
        office=("Level 9, 25 Bligh Street, Sydney NSW 2000", "ad", CBD),
        desc="""Kestrel Mobility connects commuters, councils and fleet operators through one journey-planning and ticketing platform used in six Australian cities.

The role
We are looking for a Group Product Manager to lead our rider-facing product group: the app, the web checkout and the partner portal. You will:
- Set product strategy for the group and present it to the executive team each quarter
- Line-manage three product managers and grow their craft
- Own the commercial side: fares, bundles and partner pricing
- Work with Operations and the data team to measure reliability and rider satisfaction

About you
- 7+ years in product management, including managing product managers
- Experience with marketplaces, mobility or ticketing products
- Commercial mindset, comfortable with pricing and revenue
- A track record of working with executives and external partners

Working with us
Hybrid: three days a week in our Sydney CBD office, with the rest from home. $180,000 – $200,000 plus super.""",
        fit=(77, [
            ("7+ years in product management", True, "years"),
            ("Sets product strategy and presents to executives", True, "strategy"),
            ("Commercial ownership: pricing, bundles and revenue", True, "pricing"),
            ("Line-manages three product managers", True, None),
            ("Marketplace, mobility or ticketing experience", True, None),
            ("Works with external partners", False, "stakeholders"),
        ], ["Has mentored two associate product managers but has not line-managed a team",
            "No marketplace, mobility or ticketing experience"],
            "Strong on strategy, pricing and roadmap ownership at group level. The ad requires line-managing three product managers and marketplace experience; Alex has mentored two associate PMs but not managed a team."),
    ),
    dict(
        key="kestrel_seek", company="Kestrel Mobility", title="Group Product Manager, Mobility",
        location="Sydney CBD, NSW", mode="hybrid", days=None, salary=None, source="seek",
        url=seek(81241586), age=(0, 0), run_seen=True, detail="summary", place=CBD, status="to_review",
        industry="Information & Communication Technology",
        desc="""Lead the rider-facing product group at a fast-growing mobility platform: app, web checkout and partner portal. Set strategy, manage a team of product managers and own the commercial side. Hybrid, Sydney CBD.""",
        fit=(74, [
            ("Leads a product group and manages product managers", True, None),
            ("Sets product strategy for a group", True, "strategy"),
            ("Commercial ownership", True, "pricing"),
            ("Mobility or marketplace experience", False, None),
        ], ["Only the listing summary was available: team size and seniority could not be checked",
            "No experience managing product managers or in mobility"],
            "Looks like the same role as the Kestrel Mobility careers-page ad, scored on the listing summary only. The summary stresses team management, which the documents show only as mentoring, so it scores a little below the full ad."),
    ),
    dict(
        key="mulberry", company="Mulberry Learning", title="Product Owner, Learner Experience",
        location="Ultimo, Sydney NSW", mode="hybrid", days=2, salary="$130,000 – $145,000 + super", source="seek",
        url=seek(81229340), age=(9, 4), detail="full", place=ULTIMO, status="to_review",
        industry="Education & Training",
        desc="""Mulberry Learning delivers online short courses and micro-credentials to 200,000 learners a year, in partnership with Australian universities.

About the role
You will be the product owner for the Learner Experience squad, responsible for how learners find, start and complete a course.
- Maintain a clear, prioritised backlog and run refinement, planning and review with the squad
- Turn learner research and completion data into well-defined stories
- Work with designers in Figma to shape flows that are accessible to all learners (WCAG 2.2 AA)
- Report on activation, completion and satisfaction

About you
- 3+ years as a product owner or product manager
- Education technology or online learning experience is highly regarded
- Working knowledge of accessibility standards
- Great collaboration with designers and engineers

Hybrid: two days a week in our Ultimo studio. $130,000 – $145,000 plus super.""",
        fit=(73, [
            ("3+ years as a product owner or product manager", True, "years"),
            ("Runs backlog refinement, planning and review", True, "agile"),
            ("Turns user research into stories", True, "discovery"),
            ("Designs flows with designers in Figma", True, "figma"),
            ("Education technology or online learning experience", False, None),
            ("Accessibility standards (WCAG)", True, None),
        ], ["No education technology experience", "Documents show no accessibility (WCAG) work"],
            "Reasonable match: product-owner skills, backlog work and Figma collaboration are evidenced. Education technology is new to Alex and the ad asks for accessibility experience that the documents do not show."),
    ),
    dict(
        key="quoll", company="Quoll Telecom", title="Senior Product Manager, Network Insights",
        location="Macquarie Park, Sydney NSW", mode="hybrid", days=2, salary="$150,000 – $170,000 + super", source="seek",
        url=seek(81198273), age=(18, 1), detail="full", place=MACQUARIE_PARK, status="rejected",
        industry="Telecommunications",
        desc="""Quoll Telecom supplies wholesale connectivity to retailers and enterprises. The Network Insights team builds the analytics products that help customers see how their services perform.

The role
- Own the roadmap for Network Insights, an analytics product used by enterprise and wholesale customers
- Work with data scientists to bring machine-learning features such as anomaly detection to customers
- Define requirements with network engineers and translate them for non-technical buyers
- Present to customers and support the sales team on long enterprise deals

You bring
- 5+ years in B2B product management with a data-heavy product
- SQL and comfort working with large datasets
- Telecommunications or network-monitoring experience is highly regarded
- Experience with machine-learning features is a plus

Hybrid: two days a week in our Macquarie Park office. $150,000 – $170,000 plus super.""",
        fit=(71, [
            ("5+ years in B2B product management", True, "b2b"),
            ("SQL and large datasets", True, "sql"),
            ("Enterprise customers and long sales cycles", True, "stakeholders"),
            ("Telecommunications or network-monitoring experience", False, None),
            ("Machine-learning product features", False, None),
        ], ["No telecommunications experience", "No machine-learning product experience"],
            "Good analytical and B2B match for a data product role. Telecommunications and machine-learning experience are missing, and enterprise telco buyers are a new market for Alex."),
    ),
    dict(
        key="ironbark", company="Ironbark Energy", title="Senior Product Manager, Customer App",
        location="Parramatta, NSW", mode="onsite", days=None, salary="$150,000 – $165,000 + super",
        source="site:ironbark-energy.example", src="ironbark", url="https://www.ironbark-energy.example/about/careers/senior-product-manager-customer-app",
        age=(14, 2), detail="full", place=PARRAMATTA, status="to_review",
        desc="""Ironbark Energy retails electricity and gas to 600,000 households and small businesses across New South Wales and Queensland, and is building one of the country's most-used energy apps.

The role
- Own the roadmap for the Ironbark customer app (iOS, Android and web): usage, billing, payments and outage updates
- Use experiments and analytics to raise adoption and cut calls to the contact centre
- Work with Billing, Customer Operations and Technology to ship changes safely in a regulated industry
- Lead a squad of nine and report progress to the Chief Customer Officer

About you
- 6+ years in product management, including a consumer-facing mobile app at scale
- Experience with billing or payments flows
- Strong experimentation and analytics skills
- Energy retail knowledge is a plus

This is an office-based role in Parramatta. $150,000 – $165,000 plus super.""",
        fit=(69, [
            ("6+ years in product management", True, "years"),
            ("Owns a consumer mobile app at scale", True, None),
            ("Billing or payments flows", True, "payments"),
            ("Experimentation and analytics", True, "experiments"),
            ("Leads a squad", True, "roadmap"),
            ("Energy retail knowledge", False, None),
        ], ["Documents show B2B products only, not a consumer app at scale", "No energy-retail experience"],
            "Alex has relevant payments and experimentation experience, but the role centres on a consumer mobile app at large scale and the documents show B2B products only. Energy retail is a new domain."),
    ),
    dict(
        key="fernhill", company="Fernhill Digital", title="Product Manager (6-month contract)",
        location="Chatswood, Sydney NSW", mode="hybrid", days=None, salary="$950 – $1,100 per day", source="seek",
        url=seek(81241002), age=(0, 0), run_seen=True, detail="summary", place=CHATSWOOD, status="to_review",
        industry="Consulting & Strategy",
        desc="""Digital consultancy seeks a Product Manager for a 6-month contract with a major retail client. Hybrid, Chatswood. Run discovery, define the roadmap and work with delivery squads. Immediate start. $950 - $1,100 per day.""",
        fit=(66, [
            ("Product management for a client engagement", True, "years"),
            ("Discovery and roadmap definition", True, "roadmap"),
            ("Works with delivery squads", True, "agile"),
            ("Several concurrent client engagements", False, None),
        ], ["Only the listing summary was available",
            "Documents show one in-house product at a time, not consultancy work"],
            "Alex's discovery, roadmap and delivery record fits client work, but the documents show one product at a time rather than consultancy engagements. Only the listing summary was available."),
    ),
    dict(
        key="gumnut", company="Gumnut Software", title="Technical Product Manager, Integrations",
        location="Remote - Australia", mode="remote", days=None, salary="$150,000 – $170,000 + super",
        source="greenhouse:gumnut-software", src="gumnut", url="https://gumnut-software.example/careers/technical-pm-integrations",
        age=(21, 6), detail="full", place=None, status="shortlisted",
        desc="""Gumnut Software makes workflow automation for finance teams, with more than 400 integrations to accounting, payroll and banking systems.

The role
- Own the integrations roadmap: which systems we connect to, how deep, and in what order
- Define the public API and webhooks with the platform team, and write the developer documentation priorities
- Build the partner programme with marketplaces and accounting vendors
- Work with Customer Success to find the integrations customers ask for most

About you
- 4+ years in product management on a B2B SaaS product
- A technical background: you have written code, or designed and documented APIs
- Experience with partner programmes or integration marketplaces
- Excellent written communication for a remote-first team

Fully remote within Australia.""",
        fit=(63, [
            ("4+ years in product management on B2B SaaS", True, "b2b"),
            ("Owns a roadmap", True, "roadmap"),
            ("Technical background or hands-on API design", True, None),
            ("Partner programme or integration marketplace experience", True, None),
            ("Remote-first communication", False, "remote"),
        ], ["No hands-on API design or engineering background in the documents",
            "No partner-programme or marketplace experience"],
            "Alex matches the B2B SaaS and roadmap parts of this role. It is a technical product role, and the documents do not show hands-on API design, an engineering background or partner-programme work."),
    ),
    dict(
        key="larkspur", company="Larkspur Retail Group", title="Product Manager, eCommerce",
        location="Alexandria, Sydney NSW", mode="hybrid", days=3, salary="$125,000 – $140,000 + super", source="seek",
        url=seek(81225518), age=(13, 5), detail="summary", place=ALEXANDRIA, status="not_interested",
        industry="Retail & Consumer Products",
        desc="""Join a national homewares retailer and own the online store: search, product pages, checkout and promotions. Retail eCommerce experience is essential. Hybrid: three days a week in the Alexandria office.""",
        fit=(58, [
            ("Retail eCommerce product experience", True, None),
            ("Conversion optimisation", True, "funnel"),
            ("Storefront platform experience", False, None),
        ], ["No retail or eCommerce experience in the documents", "Only the listing summary was available"],
            "Alex's conversion work transfers, but this is a retail eCommerce role and the documents show no retail or storefront-platform experience."),
    ),
    dict(
        key="redgum", company="Redgum Talent Partners", title="Product Manager | Leading Fintech | Hybrid",
        location="Sydney NSW", mode="hybrid", days=None, salary="$150,000 – $170,000", source="seek",
        url=seek(81241455), age=(0, 0), run_seen=True, detail="summary", place=CITY_CENTRE, status="to_review",
        industry="Recruitment & Talent",
        desc="""Our client, a leading fintech, is looking for a Product Manager to join its growing team. Hybrid working in Sydney. You will own a product area end to end and work with a cross-functional squad. Apply today for a confidential conversation.""",
        fit=(52, [
            ("Product management experience", True, "years"),
            ("Fintech or payments domain", False, "payments"),
            ("Level, team size and client are not stated", False, None),
        ], ["The client, level and office are not disclosed in the ad"],
            "The ad gives few details: a leading fintech client, hybrid in Sydney, no level or team size. Alex's payments background fits the stated domain, but there is too little to judge seniority or scope."),
    ),
    dict(
        key="banksia", company="Banksia Care Network", title="Product Manager, Care Coordination",
        location="Sydney NSW", mode="hybrid", days=None, salary="$135,000 – $150,000 + super", source="seek",
        url=seek(81241230), age=(0, 0), run_seen=True, detail="summary", place=CITY_CENTRE, status="to_review",
        industry="Healthcare & Medical",
        desc="""A not-for-profit aged-care and disability provider is hiring a Product Manager to improve how care workers and families coordinate. Sector knowledge and NDIS experience essential. Hybrid, Sydney.""",
        fit=(43, [
            ("Aged-care or disability sector experience (NDIS)", True, None),
            ("Care-coordination systems", True, None),
            ("Product management experience", True, "years"),
            ("Stakeholder management", False, "stakeholders"),
        ], ["No aged-care or disability-sector experience", "No care-coordination or NDIS knowledge",
            "Only the listing summary was available"],
            "Alex has the core product-management skills, but the ad requires aged-care or disability-sector experience and knowledge of care-coordination systems, and neither appears in the documents."),
    ),
    # --- Jobs the saved criteria exclude -----------------------------------
    dict(
        key="tallowood", company="Tallowood Credit Union", title="Product Manager, Digital Lending",
        location="Rhodes, Sydney NSW", mode="hybrid", days=4, salary="$135,000 – $150,000 + super", source="seek",
        url=seek(81220871), age=(16, 2), detail="full", place=RHODES, status="to_review",
        industry="Banking & Financial Services",
        desc="""Tallowood is a member-owned credit union with 90,000 members. As Product Manager, Digital Lending you will own the home-loan and personal-loan application journey in our app and online banking.

You will work with Credit, Compliance and Technology to simplify the application, shorten approvals and keep us within responsible-lending rules.

You bring 4+ years of product management in banking or fintech, and strong stakeholder skills.

Our hybrid teams work four days a week in our Rhodes office, with Fridays from home. $135,000 – $150,000 plus super.""",
        fit=None,
    ),
    dict(
        key="driftwood", company="Driftwood Travel", title="Associate Product Manager",
        location="Bondi Junction, Sydney NSW", mode="hybrid", days=None, salary="$95,000 – $110,000", source="seek",
        url=seek(81241119), age=(0, 0), run_seen=True, detail="summary", place=BONDI_JUNCTION, status="to_review",
        industry="Travel & Tourism",
        desc="""Join a boutique travel start-up as Associate Product Manager. Support the product team with research, backlog grooming and reporting. Hybrid, Bondi Junction. $95,000 - $110,000.""",
        fit=None,
    ),
    dict(
        key="luckyharbour", company="Lucky Harbour Gaming", title="Product Manager, Player Experience",
        location="Sydney CBD, NSW", mode="hybrid", days=None, salary="$150,000 – $170,000 + super", source="seek",
        url=seek(81216634), age=(20, 3), detail="full", place=BARANGAROO, status="to_review",
        industry="Gambling & Casinos",
        desc="""Lucky Harbour Gaming runs online wagering and casino games across Australia. As Product Manager, Player Experience you will own the sign-up, deposit and account journey in our mobile app.

You will run experiments, work with Compliance on responsible-gambling controls and lead a squad of eight. Hybrid working from our Sydney CBD office.""",
        fit=None,
    ),
    dict(
        key="bluestone", company="Bluestone Resources", title="Senior Product Manager, Digital Operations",
        location="Wollongong, NSW", mode="onsite", days=None, salary="$155,000 – $175,000 + super",
        source="site:bluestone-resources.example", url="https://www.bluestone-resources.example/careers/senior-pm-digital-operations",
        age=(25, 0), detail="full", place=WOLLONGONG, status="to_review",
        desc="""Bluestone Resources runs coal-handling and port operations south of Sydney. We are hiring a Senior Product Manager to lead our Digital Operations platform: scheduling, maintenance and safety reporting for the site teams.

This is an office-based role at our Wollongong head office. You will work closely with operations leaders and a team of six engineers. $155,000 – $175,000 plus super.""",
        fit=None,
    ),
    dict(
        key="meridian", company="Meridian Civic Digital", title="Senior Product Manager, Citizen Services",
        location="Parramatta, NSW", mode="onsite", days=None, salary="$145,000 – $160,000", source="seek",
        url=seek(81209915), age=(23, 4), detail="full", place=PARRAMATTA, status="to_review",
        industry="Information & Communication Technology",
        desc="""Meridian Civic Digital delivers online services for state agencies. As Senior Product Manager, Citizen Services you will lead a squad building the next version of a licensing and payments portal used by 2 million residents.

Essential: baseline security clearance required before commencement, and Australian citizenship. This is an office-based role in Parramatta. $145,000 – $160,000.""",
        fit=None,
    ),
]

# Expected: the saved criteria give these exclusion reasons (checked against the app's own rules).
EXPECTED_EXCLUDED = {
    "tallowood": "hybrid role needs 4 office days",
    "driftwood": "salary below floor",
    "luckyharbour": "dealbreaker industry: gambling",
    "bluestone": "outside your pin radii",
    "meridian": "excluded keyword: security clearance",
}

SEARCH_PROFILE = dict(
    titles=["Product Manager", "Senior Product Manager", "Product Owner", "Group Product Manager"],
    keywords_include=["roadmap", "B2B SaaS", "stakeholder management"],
    keywords_exclude=["graduate", "internship", "security clearance"],
    salary_floor=130000, remote_aus_ok=True, remote_global_ok=False, allow_hybrid=True, max_office_days=3,
    allow_onsite=True, seek_enabled=True, seek_locations=["All Sydney NSW"],
    commute_from=["Strathfield Station"], commute_arrive_by="09:00", commute_leave_at="17:00",
)
DEALBREAKERS = dict(industries=["gambling", "tobacco", "defence"], keywords=["poker machines"])


MELBOURNE = (-37.8136, 144.9631)


# --------------------------------------------------------------------------
# Company research (fictional)
# --------------------------------------------------------------------------

def _src(title: str, url: str) -> dict:
    return {"title": title, "url": url}


COMPANIES = [
    dict(
        name="Wattle Analytics", official="Wattle Analytics Pty Ltd",
        model="Sells a cloud data platform that lets mid-sized retailers, utilities and councils combine operational data and build self-serve dashboards. Revenue is annual subscriptions priced by data volume and users, plus paid onboarding.",
        ownership="Private. Founded in 2017 by two former consultants; Series B (2025) led by Greenline Ventures.",
        hq="Sydney, Australia", employees="About 140 (2026, company website)",
        offices=[("Head office", "Level 4, 12 Edward Street, Pyrmont NSW 2009", "https://wattle-analytics.example/contact", PYRMONT),
                 ("Melbourne office", "Level 3, 200 Collins Street, Melbourne VIC 3000", "https://wattle-analytics.example/contact", MELBOURNE)],
        sentiment=dict(
            summary="Employees describe a friendly, fast-moving scale-up with real ownership and a strong engineering culture. Several reviews mention that roles blur as the company grows and that planning changes quickly.",
            rating="4.2/5 on Glassdoor (64 reviews)", positives=["Supportive managers", "Interesting data problems", "Flexible hybrid working"],
            negatives=["Priorities change quickly", "Some process still being built"],
            sources=[_src("Wattle Analytics reviews", "https://reviews.example/wattle-analytics")]),
        controversies=[], note="No significant controversies found in news coverage.",
        sources=[_src("Wattle Analytics: about us", "https://wattle-analytics.example/about"),
                 _src("Wattle Analytics raises Series B", "https://news.example/wattle-analytics-series-b")],
    ),
    dict(
        name="Saltbush Payments", official="Saltbush Payments Pty Ltd",
        model="Payment processing and cash-flow tools for small and medium merchants. Earns a percentage of each transaction plus monthly fees for its software and settlement accounts.",
        ownership="Private. Majority owned by its founders; minority stake held by a venture fund, Tideline Capital.",
        hq="Sydney, Australia", employees="About 320 (2026, LinkedIn)",
        offices=[("Head office", "Level 2, 88 Foveaux Street, Surry Hills NSW 2010", "https://saltbush-payments.example/contact", SURRY_HILLS),
                 ("Melbourne office", "Level 5, 101 Flinders Lane, Melbourne VIC 3000", "https://saltbush-payments.example/contact", MELBOURNE)],
        sentiment=dict(
            summary="Reviews are mostly positive about colleagues and the product. Common concerns are on-call load for engineers and slow decisions on compliance-heavy changes.",
            rating="3.9/5 on Glassdoor (112 reviews)", positives=["Smart, helpful colleagues", "Clear mission", "Good leave"],
            negatives=["Slow compliance sign-off", "On-call load"],
            sources=[_src("Saltbush Payments reviews", "https://reviews.example/saltbush-payments")]),
        controversies=[], note="No significant controversies found in news coverage.",
        sources=[_src("Saltbush Payments: company", "https://saltbush-payments.example/company")],
    ),
    dict(
        name="Harbourline Insurance", official="Harbourline Insurance Limited",
        model="General insurer selling home, motor and small-business policies directly and through brokers. Earns premiums and investment income on reserves.",
        ownership="Public company listed on the ASX.",
        hq="Sydney, Australia", employees="About 1,900 (2025, annual report)",
        offices=[("Head office", "Level 12, 77 Walker Street, North Sydney NSW 2060", "https://harbourline-insurance.example/contact", NORTH_SYDNEY)],
        sentiment=dict(
            summary="Employees rate stability and benefits highly. Reviews describe a large organisation where change takes time, with some teams moving faster than others.",
            rating="3.7/5 on Glassdoor (240 reviews)", positives=["Job security", "Good benefits", "Hybrid working"],
            negatives=["Slow decision-making", "Legacy systems"],
            sources=[_src("Harbourline Insurance reviews", "https://reviews.example/harbourline-insurance")]),
        controversies=[], note="No significant controversies found in news coverage.",
        sources=[_src("Harbourline Insurance annual report 2025", "https://harbourline-insurance.example/investors/annual-report-2025")],
    ),
    dict(
        name="Copperleaf Health", official="Copperleaf Health Pty Ltd",
        model="Scheduling, notes and referral software for allied-health and community-care providers. Revenue is per-clinician monthly subscriptions plus payment processing.",
        ownership="Private. Backed by a venture fund and by the founders.",
        hq="Melbourne, Australia", employees="51-200 (LinkedIn)",
        offices=[("Head office", "Level 6, 33 Queen Street, Melbourne VIC 3000", "https://copperleaf-health.example/contact", MELBOURNE)],
        sentiment=dict(
            summary="Few reviews, mostly positive about a remote-friendly culture and a mission-driven team. Some mention stretched support during a fast-growth year.",
            rating="4.4/5 on Glassdoor (19 reviews)", positives=["Remote-first", "Mission-driven team"],
            negatives=["Stretched support team"],
            sources=[_src("Copperleaf Health reviews", "https://reviews.example/copperleaf-health")]),
        controversies=[], note="No significant controversies found in news coverage.",
        sources=[_src("Copperleaf Health: about", "https://copperleaf-health.example/about")],
    ),
    dict(
        name="Kestrel Mobility", official="Kestrel Mobility Pty Ltd",
        model="Journey-planning and ticketing platform for commuters, councils and fleet operators. Earns a share of ticket sales plus licence fees from councils and operators.",
        ownership="Private. Owned by its founders and an infrastructure fund, Rail & Road Partners.",
        hq="Sydney, Australia", employees="About 260 (2026, company website)",
        offices=[("Head office", "Level 9, 25 Bligh Street, Sydney NSW 2000", "https://kestrel-mobility.example/contact", CBD)],
        sentiment=dict(
            summary="Reviewers like the product mission and the CBD location. Several mention long hours around city launches.",
            rating="3.8/5 on Glassdoor (58 reviews)", positives=["Meaningful product", "Central location"],
            negatives=["Long hours around launches"],
            sources=[_src("Kestrel Mobility reviews", "https://reviews.example/kestrel-mobility")]),
        controversies=[], note="No significant controversies found in news coverage.",
        sources=[_src("Kestrel Mobility: our story", "https://kestrel-mobility.example/story")],
    ),
    dict(
        name="Mulberry Learning", official="Mulberry Learning Pty Ltd",
        model="Online short courses and micro-credentials delivered with university partners. Learners pay course fees; Mulberry keeps a share and the partner receives the rest.",
        ownership="Private, owned by its founders and an education-focused investor.",
        hq="Sydney, Australia", employees="51-200 (LinkedIn)",
        offices=[("Studio", "Level 1, 18 Mary Ann Street, Ultimo NSW 2007", "https://mulberry-learning.example/contact", ULTIMO)],
        sentiment=dict(
            summary="Employees are positive about colleagues and flexible hours. Pay is described as fair rather than generous.",
            rating="4.0/5 on Glassdoor (31 reviews)", positives=["Friendly team", "Flexible hours"], negatives=["Pay is fair, not high"],
            sources=[_src("Mulberry Learning reviews", "https://reviews.example/mulberry-learning")]),
        controversies=[], note="No significant controversies found in news coverage.",
        sources=[_src("Mulberry Learning: about", "https://mulberry-learning.example/about")],
    ),
    dict(
        name="Ironbark Energy", official="Ironbark Energy Retail Pty Ltd",
        model="Electricity and gas retailer for households and small businesses in New South Wales and Queensland. Earns the margin between wholesale energy costs and the prices it charges customers.",
        ownership="Subsidiary of Ironbark Group Holdings, which is privately owned by a superannuation fund consortium.",
        hq="Parramatta, Australia", employees="About 1,400 (2025, company website)",
        offices=[("Head office", "Level 7, 1 Smith Street, Parramatta NSW 2150", "https://ironbark-energy.example/contact", PARRAMATTA)],
        sentiment=dict(
            summary="Reviews are mixed: people like their teams and the pay, but mention legacy systems and a heavy workload in customer-facing areas after a platform migration.",
            rating="3.4/5 on Glassdoor (150 reviews)", positives=["Supportive teams", "Competitive pay"],
            negatives=["Legacy systems", "Heavy workload in customer teams"],
            sources=[_src("Ironbark Energy reviews", "https://reviews.example/ironbark-energy")]),
        controversies=[dict(
            title="Billing delays after a platform migration", year="2025",
            summary="Some customers reported delayed or estimated bills for several weeks after the retailer moved to a new billing system. The company apologised and credited affected accounts; no regulatory action was reported.",
            sources=[_src("Ironbark customers hit by delayed bills", "https://news.example/ironbark-billing-delays")])],
        note="One operational incident reported; no fines or investigations found.",
        sources=[_src("Ironbark Energy: who we are", "https://ironbark-energy.example/about")],
    ),
]


# --------------------------------------------------------------------------
# Building the database
# --------------------------------------------------------------------------

def build(db_path: Path) -> None:
    isolate(db_path)

    from backend.db import (AppSetting, ChatMessage, ChatThread, CompanyProfile, CompanySource, CoverLetter,
                            DealbreakerSet, Document, FitResult, GeocodeCache, Job, JobDuplicate, Pin, SearchRun,
                            SessionLocal, UserProfile, Workspace, company_key, init_db)
    from backend.duplicates import scan
    from backend.filters import commute_pins, exclusion_reason, load_criteria, parse_salary
    from backend.geo import _geocode_query, classify_work_mode, home_distance, parse_office_days
    from backend.offices import office_queries, office_unknown
    from backend.schedule import ScheduleSchema, _tz, slots_on
    from backend.scorer import build_profile
    from backend.scraping.text import description_hash

    init_db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def ago(days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
        return now - timedelta(days=days, hours=hours, minutes=minutes)

    # The latest scheduled search, from the preset schedule (weekday slots, public holidays skipped).
    sched = ScheduleSchema()
    clock = datetime.now(timezone.utc)
    slot = None
    for back in range(0, 14):
        day = clock.astimezone(_tz(sched)).date() - timedelta(days=back)
        past = [s for s in slots_on(sched, day) if s <= clock - timedelta(minutes=10)]
        if past:
            slot = past[-1]
            break
    run_start = slot.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(seconds=14)
    run_end = run_start + timedelta(minutes=4, seconds=31)

    db = SessionLocal()
    try:
        ws = db.get(Workspace, 1)
        ws.name = WORKSPACE_NAME
        ws_id = ws.id

        # --- search profile, dealbreakers, pins --------------------------------
        db.add(UserProfile(id=ws_id, **SEARCH_PROFILE))
        db.add(DealbreakerSet(id=ws_id, **DEALBREAKERS))
        for label, kind, (lat, lng), radius in PINS:
            db.add(Pin(workspace_id=ws_id, label=label, kind=kind, lat=lat, lng=lng, radius_km=radius))

        # --- documents (text only: nothing is written to data/uploads) -----------
        db.add(Document(workspace_id=ws_id, filename=CV_FILENAME, filetype=".pdf", stored_name=None, text=CV_TEXT,
                        kind="resume", use_for_scoring=True, created_at=ago(days=24)))
        db.add(Document(workspace_id=ws_id, filename=OLD_LETTER_FILENAME, filetype=".docx", stored_name=None,
                        text=OLD_LETTER_TEXT, kind="cover_letter", use_for_scoring=True, created_at=ago(days=24, hours=-1)))
        db.commit()
        _, phash = build_profile(db, ws_id)

        # --- company sources ------------------------------------------------------
        source_rows = {}
        source_report = {  # per-source numbers of the latest scheduled run
            "wattle": (6, 0, 0, 1, 5), "saltbush": (11, 0, 0, 1, 10), "copperleaf": (9, 1, 1, 1, 8),
            "kestrel": (17, 0, 0, 1, 16), "ironbark": (0, 0, 0, 0, 0), "gumnut": (8, 0, 0, 1, 7),
        }  # listed, new, details fetched, kept, excluded
        for key, (label, url, method) in SOURCES.items():
            error = "HTTP 403: www.ironbark-energy.example refused automated access" if key == "ironbark" else None
            row = CompanySource(workspace_id=ws_id, label=label, careers_url=url, url=url, source_type="auto",
                                enabled=True, last_run=run_start + timedelta(seconds=40 + 20 * len(source_rows)),
                                last_count=source_report[key][0], last_error=error,
                                last_method=None if error else method)
            db.add(row)
            source_rows[key] = row
        db.commit()

        # --- jobs -----------------------------------------------------------------
        crit = load_criteria(db, ws_id)
        pins = commute_pins(crit.pins)
        jobs: dict[str, Job] = {}
        reasons: dict[str, str] = {}
        for spec in JOBS:
            desc = spec["desc"].strip()
            days, hours = spec["age"]
            first_seen = run_start + timedelta(seconds=45 + 11 * len(jobs)) if spec.get("run_seen") else ago(days, hours)
            lat, lng = spec["place"] if spec["place"] else (None, None)
            office = spec.get("office")
            if office:
                text, source, (olat, olng) = office
                lat, lng = olat, olng
            mode = spec["mode"]
            mode_from_text = classify_work_mode(spec["title"], spec["location"], desc)
            if mode_from_text != mode:
                raise SystemExit(f"{spec['key']}: the ad reads as {mode_from_text!r}, not {mode!r}; reword the ad")
            days_in_office = parse_office_days(spec["title"], spec["location"], desc)
            if days_in_office != spec["days"]:
                raise SystemExit(f"{spec['key']}: the ad says {days_in_office} office days, expected {spec['days']}")
            salary_min = salary_max = None
            if spec["source"] != "seek" and spec.get("salary"):
                salary_min, salary_max = (int(v) for v in parse_salary(spec["salary"]))
            job = Job(
                workspace_id=ws_id, source=spec["source"], source_id=source_rows[spec["src"]].id if spec.get("src") else None,
                external_id=spec["url"].rsplit("/", 1)[-1], company=spec["company"], title=spec["title"],
                location_text=spec["location"], country="AU", lat=lat, lng=lng, work_mode=mode, office_days=days_in_office,
                salary_min=salary_min, salary_max=salary_max, salary_text=spec.get("salary"), url=spec["url"],
                description=desc, description_hash=description_hash(desc), detail_status=spec["detail"],
                detail_fetched_at=first_seen + timedelta(minutes=2) if spec["detail"] == "full" else None,
                posted_at=(first_seen - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                industry=spec.get("industry"), distance_km=home_distance(lat, lng, pins), status=spec["status"],
                first_seen=first_seen, last_seen=run_start + timedelta(seconds=30),
            )
            if office:
                job.office_text, job.office_source, job.office_lat, job.office_lng = text, source, olat, olng
                job.office_checked_at = first_seen + timedelta(minutes=12)
            reason = exclusion_reason(job, crit, stage="full")
            job.excluded_reason = reason
            reasons[spec["key"]] = reason or ""
            db.add(job)
            jobs[spec["key"]] = job
        db.flush()

        for key, wanted in EXPECTED_EXCLUDED.items():
            if wanted not in reasons[key]:
                raise SystemExit(f"{key}: expected an exclusion containing {wanted!r}, got {reasons[key]!r}")
        for key, reason in reasons.items():
            if reason and key not in EXPECTED_EXCLUDED:
                raise SystemExit(f"{key} is unexpectedly excluded: {reason}")
        if not office_unknown(jobs["redgum"]) or not office_unknown(jobs["banksia"]):
            raise SystemExit("the two city-only ads should read as 'office unknown'")
        if office_unknown(jobs["saltbush"]) or office_unknown(jobs["wattle"]):
            raise SystemExit("jobs with a known office should not read as 'office unknown'")

        # --- fit results (scored against the current documents, so none are stale) ----
        for spec in JOBS:
            if not spec["fit"]:
                continue
            score, reqs, gaps, summary = spec["fit"]
            requirements = []
            for point, must, key in reqs:
                evidence = []
                if key:
                    bullet, subs = EVIDENCE[key]
                    evidence = [{"bullet": bullet, "sub_bullets": subs}]
                requirements.append({"point": point, "must_have": must, "matched": bool(key), "evidence": evidence})
            job = jobs[spec["key"]]
            db.add(FitResult(job_id=job.id, status="ok", score=score, profile_hash=phash, model="example-model",
                             created_at=job.first_seen + timedelta(minutes=2),
                             evidence_json=json.dumps({"score": score, "requirements": requirements, "gaps": gaps,
                                                       "summary": summary})))

        # --- company research --------------------------------------------------------
        for i, c in enumerate(COMPANIES):
            db.add(CompanyProfile(
                key=company_key(c["name"]), name=c["name"], status="done", official_name=c["official"],
                is_recruiter=False, business_model=c["model"], ownership=c["ownership"], headquarters=c["hq"],
                controversies_json=json.dumps(c["controversies"]), controversy_note=c["note"],
                sources_json=json.dumps(c["sources"]), employee_count=c["employees"],
                glassdoor_url=f"https://reviews.example/{company_key(c['name']).replace(' ', '-')}",
                sentiment_json=json.dumps(c["sentiment"]), research_version=3,
                offices_json=json.dumps([{"name": n, "address": a, "url": u} for n, a, u, _xy in c["offices"]]),
                offices_checked_at=ago(days=20 + i), offices_cities=json.dumps(["sydney"]),
                model="example-model", researched_at=ago(days=20 + i)))
        db.add(CompanyProfile(
            key=company_key("Redgum Talent Partners"), name="Redgum Talent Partners", status="done",
            official_name="Redgum Talent Partners Pty Ltd", is_recruiter=True,
            business_model="Recruitment agency placing technology and product professionals with employers on permanent and contract terms.",
            ownership="Private.", headquarters="Sydney, Australia", employee_count="51-200 (LinkedIn)",
            controversies_json="[]", controversy_note="No significant controversies found in news coverage.",
            sources_json=json.dumps([_src("Redgum Talent Partners: about", "https://redgum-talent.example/about")]),
            sentiment_json=json.dumps(dict(summary="Few reviews.", rating="", positives=[], negatives=[], sources=[])),
            research_version=3, offices_json="[]", offices_checked_at=ago(days=20), offices_cities=json.dumps(["sydney"]),
            model="example-model", researched_at=ago(days=20)))

        # --- cover letter + chat (for the top job) -------------------------------------
        top = jobs["wattle"]
        db.add(CoverLetter(job_id=top.id, text=COVER_LETTER, instructions="Keep it under 300 words.", edited=True,
                           model="example-model", reasoning_effort="medium", created_at=ago(days=10, hours=2),
                           updated_at=ago(days=10)))
        thread = ChatThread(workspace_id=ws_id, job_ids_json=json.dumps([top.id]), created_at=ago(days=3),
                            title=CHAT[0][1], updated_at=ago(days=3) + timedelta(minutes=6))
        db.add(thread)
        db.flush()
        for i, (role, text) in enumerate(CHAT):
            db.add(ChatMessage(thread_id=thread.id, role=role, content=text, web_search=False,
                               model="example-model" if role == "assistant" else None,
                               created_at=ago(days=3) + timedelta(minutes=3 * i)))

        # --- geocoder cache: the only lookups a job page could make -----------------------
        def cache(query: str, lat: float, lng: float, name: str, kind: str) -> None:
            db.merge(GeocodeCache(query=query.lower(), lat=lat, lng=lng, country="AU", display_name=name,
                                  place_type=kind, fetched_at=now))

        cache("sydney nsw", *CITY_CENTRE, "Sydney, New South Wales, Australia", "city")
        for c in COMPANIES:
            for _name, address, _url, (lat, lng) in c["offices"]:
                cache(_geocode_query(office_queries(address, jobs["saltbush"])[0]), lat, lng, address, "building")

        # --- app settings: pretend the model's reasoning levels were detected already --------
        db.merge(AppSetting(key="reasoning_levels",
                            value_json=json.dumps({"https://llm.example.com/v1|example-model": ["low", "medium", "high"]})))
        db.commit()

        # --- duplicates: the app's own scan merges the sure pair and lists the doubtful one ----
        result = scan(db, ws_id)
        if result != {"merged": 1, "suggested": 1}:
            raise SystemExit(f"duplicate scan gave {result}; expected one merge and one suggestion")
        for row in db.query(JobDuplicate).filter(JobDuplicate.workspace_id == ws_id):
            row.created_at = run_start + timedelta(minutes=3)
            row.decided_at = run_start + timedelta(minutes=3) if row.status == "merged" else None
        merged_copy = db.get(Job, jobs["wattle_seek"].id)
        if merged_copy.duplicate_of != jobs["wattle"].id:
            raise SystemExit("the SEEK copy of the Wattle ad was not merged into the careers-page ad")

        # --- the latest scheduled search (shown on the Jobs tab) ---------------------------------
        per_source = [dict(label="SEEK", url="https://www.seek.com.au", listed=61, new=6, details_fetched=0,
                           details_deferred=0, detail_errors=0, excluded=52, kept=9, closed=0, error=None, notes=[],
                           method="SEEK search result pages, first page per search (job pages and pagination are off-limits per robots.txt)")]
        for key, (label, url, method) in SOURCES.items():
            listed, new, fetched, kept, excluded = source_report[key]
            error = source_rows[key].last_error
            per_source.append(dict(label=label, url=url, method="" if error else method, listed=listed, new=new,
                                   details_fetched=fetched, details_deferred=0, detail_errors=0, excluded=excluded,
                                   kept=kept, closed=0, error=error, notes=[]))
        totals = {k: sum(r[k] for r in per_source)
                  for k in ("listed", "new", "details_fetched", "details_deferred", "excluded", "kept", "closed")}
        db.add(SearchRun(
            workspace_id=ws_id, kind="search", scheduled=True, state="done", stage="done", progress_json="{}",
            started_at=run_start, finished_at=run_end,
            summary_json=json.dumps({
                "search": {"per_source": per_source, "totals": totals},
                "duplicates": result,
                "scoring": {"requested": 5, "unique_postings": 5, "scored": 5, "errors": 0, "skipped": 0,
                            "first_error": None, "aborted": None},
            })))
        db.commit()

        # --- final checks --------------------------------------------------------------------------
        stale = db.query(FitResult).filter(FitResult.profile_hash != phash).count()
        if stale:
            raise SystemExit(f"{stale} fit results would show as stale")
        listed = db.query(Job).filter(Job.workspace_id == ws_id, Job.duplicate_of.is_(None)).count()
        shown = db.query(Job).filter(Job.workspace_id == ws_id, Job.duplicate_of.is_(None),
                                     Job.excluded_reason.is_(None)).count()
        print(f"Demo database ready: {db_path}")
        print(f"  workspace {WORKSPACE_NAME!r}: {listed} jobs listed ({shown} pass the filters, {listed - shown} excluded), "
              f"1 merged copy, {db.query(FitResult).count()} fit scores")
        print(f"  latest scheduled search: {run_start:%Y-%m-%d %H:%M} UTC; duplicate scan {result}")
    finally:
        db.close()


COVER_LETTER = """Dear Hiring Manager,

Wattle Analytics gives data teams at retailers and councils the tools they used to wait weeks for. That is the problem I work on at Brightfield Logistics Software, where I own the roadmap for a freight-visibility platform used by 90 customers, across three squads and 24 engineers.

Two pieces of that work are most relevant to this role. I shipped a public reporting API with our data engineers, and 35% of enterprise customers now use it. I also led the pricing and packaging redesign that lifted average revenue per account by 12%, working closely with Finance and Sales. Both started the way I would approach your platform: interviews with the people who rely on the product, then SQL to size the opportunity before anything reached the roadmap.

Earlier, at Tidewater Payments, I launched merchant onboarding and cut the time to first payment from 21 days to 9. I like work where a clear metric, a regulated environment and many stakeholders pull in different directions. I have mentored two associate product managers and would enjoy doing more of that at Wattle.

I have not worked with retail or utility customers, so I would spend my first weeks with your customers and your support team. I would welcome a conversation at your convenience.

Kind regards,
Alex Sample"""

CHAT = [
    ("user", "Honestly, how well do I fit? What are my biggest gaps?"),
    ("assistant", """**Short answer: a strong fit, and the automated score of 92 is fair.**

**Where you are strongest**
- **Platform ownership:** you run a three-squad roadmap at Brightfield, the same shape as Wattle's Data Platform area.
- **Data fluency:** daily SQL plus the reporting API you shipped with data engineering speaks directly to their self-serve reporting layer.
- **Pricing:** the 12% revenue-per-account lift is good evidence for the packaging part of the role.

**Gaps to prepare for**
- **Domain:** their customers are retailers, utilities and councils. You have logistics and payments. Have a view on how data teams in those sectors differ.
- **Ingestion and modelling:** your API work is on the reporting side. Expect a question on how you would weigh pipeline reliability against new connectors.
- **People leadership:** the ad lists mentoring as a plus. You have mentored two associate PMs but not line-managed anyone; say so plainly and describe what you learned.

Your cover letter already names the domain gap, which is the right call."""),
    ("user", "What should I ask them in the interview?"),
    ("assistant", """Questions that show you have read the ad and want to understand how the area really works:

1. How do the three squads split ownership today (ingestion, modelling, reporting), and where do priority disputes usually arise?
2. Which customer segment drives most of this year's growth, and how much of the roadmap is committed to specific accounts?
3. How does the Data Platform PM work with Sales on pricing and packaging? Who has the final say?
4. What does a good first 90 days look like, and which metric would you expect me to move?
5. The ad mentions mentoring other PMs. Is there a team to lead now, or is that something I would grow into?

Ask question 5 last: the answer tells you whether this is a lead role or a senior individual-contributor role."""),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill a new database with fictional README-screenshot data.")
    parser.add_argument("db", nargs="?", default=os.environ.get("JOBHUNT_DEMO_DB"),
                        help="path of the NEW database file (or set JOBHUNT_DEMO_DB)")
    parser.add_argument("--overwrite", action="store_true", help="replace the file if it already exists")
    args = parser.parse_args()
    if not args.db:
        parser.error("give the path of a new database file, or set JOBHUNT_DEMO_DB")
    build(target_path(args.db, args.overwrite))


if __name__ == "__main__":
    main()
