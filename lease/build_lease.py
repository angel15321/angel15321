"""Generate the Hidalgo Property Management LLC fillable residential lease
(Arizona) and the landlord's new-tenant checklist.

Usage:  python3 lease/build_lease.py
Output: lease/Hidalgo_Residential_Lease.pdf
        lease/Landlord_New_Tenant_Checklist.pdf
"""
import io
import os

from reportlab.lib.colors import Color, black, HexColor
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

COMPANY = "HIDALGO PROPERTY MANAGEMENT LLC"
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

PAGE_W, PAGE_H = letter
LM, RM = 50, 50
TOP, BOTTOM = 742, 70
TEXT_W = PAGE_W - LM - RM

FIELD_FILL = Color(0.90, 0.94, 1.0)
FIELD_BORDER = HexColor("#7a8aa8")
ACCENT = HexColor("#1f3a5f")

BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=9, leading=11.6,
                      alignment=TA_JUSTIFY)
BODY_SMALL = ParagraphStyle("small", parent=BODY, fontSize=8, leading=10)
BULLET = ParagraphStyle("bullet", parent=BODY, leftIndent=14, bulletIndent=4)


class Doc:
    """Tiny flowing-layout engine on a reportlab canvas with AcroForm fields."""

    def __init__(self, stream, title, total_pages=None, footer=True):
        self.c = canvas.Canvas(stream, pagesize=letter)
        self.c.setTitle(title)
        self.c.setAuthor(COMPANY)
        self.title = title
        self.total = total_pages
        self.footer = footer
        self.page = 1
        self.n = 0
        self._start_page()

    # ---------- page handling ----------
    def _start_page(self):
        c = self.c
        self.y = TOP
        c.setFillColor(ACCENT)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(LM, PAGE_H - 34, COMPANY)
        c.setFont("Helvetica", 8)
        c.drawRightString(PAGE_W - RM, PAGE_H - 34, self.title)
        c.setStrokeColor(ACCENT)
        c.setLineWidth(0.8)
        c.line(LM, PAGE_H - 40, PAGE_W - RM, PAGE_H - 40)
        c.setFillColor(black)

    def _end_page(self):
        c = self.c
        c.setStrokeColor(ACCENT)
        c.setLineWidth(0.5)
        c.line(LM, 52, PAGE_W - RM, 52)
        c.setFont("Helvetica", 7.5)
        tot = self.total if self.total else "?"
        c.drawString(LM, 40, f"Page {self.page} of {tot}")
        if self.footer:
            c.drawRightString(PAGE_W - RM - 236, 40, "Tenant initials:")
            for i in range(2):
                self.text_field(f"init_t_p{self.page}_{i}", PAGE_W - RM - 230 + i * 44,
                                34, 38, 14, size=8)
            c.drawRightString(PAGE_W - RM - 54, 40, "Landlord/Agent:")
            self.text_field(f"init_l_p{self.page}", PAGE_W - RM - 48, 34, 48, 14, size=8)

    def new_page(self):
        self._end_page()
        self.c.showPage()
        self.page += 1
        self._start_page()

    def ensure(self, h):
        if self.y - h < BOTTOM:
            self.new_page()

    def finish(self):
        self._end_page()
        self.c.save()

    # ---------- primitives ----------
    def name(self, base):
        self.n += 1
        return f"{base}_{self.n}"

    def text_field(self, name, x, y, w, h, size=9, multiline=False, value=""):
        self.c.acroForm.textfield(
            name=name, x=x, y=y, width=w, height=h, fontName="Helvetica",
            fontSize=0 if multiline else size, borderStyle="underlined",
            borderColor=FIELD_BORDER, fillColor=FIELD_FILL, textColor=black,
            borderWidth=0.6, value=value, forceBorder=True,
            fieldFlags="multiline" if multiline else "")

    def checkbox(self, name, x, y, size=9, checked=False):
        self.c.acroForm.checkbox(
            name=name, x=x, y=y, size=size, buttonStyle="check",
            borderColor=FIELD_BORDER, fillColor=FIELD_FILL, textColor=black,
            borderWidth=0.6, checked=checked, forceBorder=True)

    # ---------- flowing content ----------
    def space(self, h=6):
        self.y -= h

    def para(self, text, style=BODY, after=4, indent=0):
        p = Paragraph(text, style)
        w, h = p.wrap(TEXT_W - indent, 1000)
        self.ensure(h)
        p.drawOn(self.c, LM + indent, self.y - h)
        self.y -= h + after

    def bullet(self, text, after=2):
        self.para(text, BULLET if not text.startswith("<b>") else BULLET, after=after)

    def section(self, title, after=3):
        self.ensure(75)
        self.y -= 5
        c = self.c
        c.setFillColor(ACCENT)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(LM, self.y - 10, title.upper())
        c.setFillColor(black)
        self.y -= 14 + after

    def row(self, items, h=13, after=5, x0=None):
        """items: list of (label, field_base, width) ; width None = fill rest.
        A label with field_base None is plain text. ('[]', base, label) => checkbox."""
        self.ensure(h + 2)
        c = self.c
        x = LM if x0 is None else x0
        ybase = self.y - h
        names = []
        for i, it in enumerate(items):
            if it[0] == "[]":
                _, base, label = it
                nm = self.name(base)
                self.checkbox(nm, x, ybase + 1.5, size=9)
                c.setFont("Helvetica", 9)
                c.drawString(x + 12, ybase + 3, label)
                x += 12 + c.stringWidth(label, "Helvetica", 9) + 12
                names.append(nm)
                continue
            label, base, width = it
            start = x
            if label:
                font = "Helvetica-Bold" if label.endswith(":") and i == 0 else "Helvetica"
                c.setFont(font, 9)
                c.drawString(x, ybase + 3, label)
                x += c.stringWidth(label, font, 9) + 4
                if not base and width:
                    x = start + width
            if base:
                if width is None:
                    rest = [w for (_l, b, w) in items[i + 1:] if b and _l != "[]"]
                    width = (LM + TEXT_W) - x - 4
                    # leave room for following fixed items
                    for (_l, b, w) in items[i + 1:]:
                        if _l == "[]":
                            continue
                        width -= (c.stringWidth(_l or "", "Helvetica", 9) + 8 + (w or 0))
                nm = self.name(base)
                self.text_field(nm, x, ybase, width, h)
                x += width + 8
                names.append(nm)
        self.y -= h + after
        return names

    def multiline(self, base, lines=3, after=6):
        h = lines * 12 + 4
        self.ensure(h)
        self.text_field(self.name(base), LM, self.y - h, TEXT_W, h, multiline=True)
        self.y -= h + after

    def initials_line(self, label="Tenant initials:"):
        self.ensure(18)
        c = self.c
        c.setFont("Helvetica-Bold", 8.5)
        xr = PAGE_W - RM
        c.drawRightString(xr - 100, self.y - 11, label)
        for i in range(2):
            self.text_field(self.name("ack_init"), xr - 94 + i * 48, self.y - 14, 44, 13)
        self.y -= 20

    def signature_block(self, who, base):
        self.ensure(40)
        c = self.c
        y = self.y - 24
        c.setLineWidth(0.6)
        c.setStrokeColor(black)
        c.line(LM, y, LM + 250, y)
        c.setFont("Helvetica", 7.5)
        c.drawString(LM, y - 9, f"{who} signature")
        self.text_field(self.name(base + "_print"), LM + 262, y, 170, 14)
        c.drawString(LM + 262, y - 9, "Printed name")
        self.text_field(self.name(base + "_date"), LM + 444, y, 68, 14)
        c.drawString(LM + 444, y - 9, "Date")
        self.y -= 44


# =====================================================================
#  LEASE CONTENT
# =====================================================================

def lease(d: Doc):
    c = d.c
    c.setFont("Helvetica-Bold", 17)
    c.setFillColor(ACCENT)
    c.drawCentredString(PAGE_W / 2, d.y - 16, "RESIDENTIAL LEASE AGREEMENT")
    c.setFont("Helvetica", 9.5)
    c.setFillColor(black)
    c.drawCentredString(PAGE_W / 2, d.y - 30, "State of Arizona  •  " + COMPANY)
    d.y -= 44

    d.para("<b>Notice to Tenant:</b> This is a legally binding agreement. Read all of it before "
           "signing. Your rights and duties are governed by this Lease and the Arizona Residential "
           "Landlord and Tenant Act, A.R.S. § 33-1301 et seq. (the “ARLTA”). A free copy "
           "of the ARLTA is available from the Arizona Department of Housing (housing.az.gov). "
           "You may consult an attorney before signing.", BODY_SMALL, after=6)

    # 1 parties / disclosure
    d.section("1. Parties and Required Disclosures (A.R.S. § 33-1322)")
    d.para("<b>Owner / Landlord</b> (the person or entity that owns the Premises):")
    d.row([("Name:", "owner_name", None)])
    d.row([("Address:", "owner_addr", None)])
    d.para("<b>Property Manager</b> (authorized to manage the Premises and to act for the Landlord):")
    d.ensure(18)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(LM, d.y - 10, "Company:")
    c.setFont("Helvetica", 9)
    c.drawString(LM + 48, d.y - 10, "Hidalgo Property Management LLC")
    d.y -= 18
    d.row([("Address:", "pm_addr", None)])
    d.row([("Phone:", "pm_phone", 150), ("Email:", "pm_email", None)])
    d.para("The Property Manager named above is authorized to receive notices, demands and "
           "service of process for the Landlord. If no Property Manager is named, the Owner "
           "receives them at the Owner address above.", after=4)
    d.para("<b>Tenant(s)</b> — every adult (18+) who will live at the Premises must be listed "
           "and must sign this Lease:")
    for i in range(1, 5):
        d.row([(f"{i}.", f"tenant{i}_name", 230), ("Phone:", f"tenant{i}_phone", 100),
               ("Email:", f"tenant{i}_email", None)], after=4)
    d.para("“Landlord” in this Lease means the Owner and the Property Manager acting for the Owner.",
           after=2)

    # 2 premises
    d.section("2. Premises")
    d.row([("Address:", "prem_addr", 300), ("Unit:", "prem_unit", None)])
    d.row([("City:", "prem_city", 220), ("Arizona   ZIP:", "prem_zip", 80)])
    d.para("Included appliances / personal property, to be kept in working order by Landlord:")
    d.row([("[]", "app", "Refrigerator"), ("[]", "app", "Range/Oven"), ("[]", "app", "Microwave"),
           ("[]", "app", "Dishwasher"), ("[]", "app", "Washer"), ("[]", "app", "Dryer")])
    d.row([("[]", "app", "Storage room"), ("[]", "app", "Garage"), ("Other:", "app_other", None)])
    d.row([("Parking: ", None, None), ("Assigned spaces / location:", "parking", None)])

    # 3 occupancy
    d.section("3. Occupants and Guests")
    d.para("The Premises may be used only as a private residence and only by the Tenant(s) above and "
           "these additional occupants (minors and others):")
    d.row([("Name:", "occ1", 220), ("Name:", "occ2", None)], after=4)
    d.row([("Name:", "occ3", 220), ("Name:", "occ4", None)])
    d.para("No one else may live at the Premises without Landlord’s prior written consent. A guest "
           "who stays more than <b>fourteen (14) days</b> in any six (6) month period is an occupant "
           "and needs written consent. Tenant may not sublet, assign this Lease, or rent any part of "
           "the Premises (including short-term rentals) without Landlord’s written consent. "
           "Breaking this section is a material noncompliance with this Lease.")

    # 4 term
    d.section("4. Lease Term")
    d.row([("Start date:", "start_date", 110), ("End date:", "end_date", 110),
           ("Term (months):", "term_months", None)])
    d.para("When the term ends, this Lease continues <b>month-to-month</b> on the same terms unless "
           "either party gives written notice to end it at least <b>thirty (30) days</b> before the "
           "last day of the term (or, while month-to-month, at least 30 days before the next rent "
           "due date). At the end of the tenancy Tenant must return all keys and openers and vacate.")
    d.para("<b>Holdover:</b> If Tenant willfully stays after the tenancy ends, Landlord may recover up "
           "to two (2) months’ rent or twice the actual damages, whichever is greater, as allowed by "
           "A.R.S. § 33-1375.")

    # 5 rent
    d.section("5. Rent")
    d.row([("Monthly rent: $", "rent", 100), ("due on or before day", "due_day", 34),
           ("of each month by 5:00 p.m.", None, None)])
    d.para("Rent is due in advance, without demand and without deductions, even if the due date falls "
           "on a weekend or holiday. (Arizona cities may no longer charge rental sales tax on "
           "residential leases as of January 1, 2025, so no tax is added to rent.)")
    d.para("Accepted payment methods:")
    d.row([("[]", "pay", "Online portal"), ("[]", "pay", "Bank transfer / ACH"),
           ("[]", "pay", "Cashier’s check"), ("[]", "pay", "Money order"),
           ("[]", "pay", "Personal check")])
    d.row([("[]", "pay", "Other:"), ("", "pay_other", None)])
    d.row([("Pay to / where:", "pay_where", None)])
    d.para("<b>Prorated first month:</b> If the Lease starts on a day other than the rent due day:")
    d.row([("Prorated rent $", "prorate_amt", 80), ("for", None, None), ("", "prorate_from", 80),
           ("through", None, None), ("", "prorate_to", 80), (", due on", None, None),
           ("", "prorate_due", None)])
    d.para("Landlord does not have to accept partial payments. Accepting a late or partial payment "
           "does not waive any right of Landlord, change the due date, or excuse the balance owed.")

    # 6 late
    d.section("6. Late Charges and Returned Payments")
    d.row([("Late fee: $", "late_fee", 70), ("if rent is not received by 5:00 p.m. on day", "late_day", 34),
           ("of the month.", None, None)])
    d.row([("Returned / dishonored payment fee: $", "nsf_fee", 70),
           ("plus the late fee, if rent is then late.", None, None)])
    d.para("These charges are reasonable estimates of Landlord’s costs and are collectible as rent. "
           "After any returned payment, Landlord may require all future payments by cashier’s check "
           "or money order.")

    # 7 deposits
    d.section("7. Deposits and Fees (A.R.S. § 33-1321)")
    d.para("<b>Refundable deposits</b> secure Tenant’s performance of this Lease:")
    d.row([("Security deposit: $", "dep_sec", 90), ("Pet deposit: $", "dep_pet", 80),
           ("Cleaning deposit: $", "dep_clean", None)])
    d.para("<b>Non-refundable fees</b> (these are <u>not</u> refundable and are stated here as required "
           "by A.R.S. § 33-1321(B)):")
    d.row([("Cleaning fee: $", "fee_clean", 80), ("Redecorating fee: $", "fee_redec", 80),
           ("Pet fee: $", "fee_pet", None)])
    d.row([("Other fee: $", "fee_other", 80), ("for:", "fee_other_for", None)])
    d.row([("Application / screening fee (non-refundable): $", "fee_app", 80),
           ("per adult applicant", None, None)])
    d.para("Total refundable deposits plus any prepaid rent may not exceed one and one-half (1½) "
           "months’ rent, unless Tenant voluntarily prepays rent. <b>Refundable deposits may not be "
           "used as last month’s rent.</b> Within <b>fourteen (14) business days</b> after the tenancy "
           "ends, possession is returned, and Tenant demands it, Landlord will return the refundable "
           "deposits less lawful deductions, with an itemized list of any deductions, mailed to the "
           "forwarding address Tenant provides. Deductions may cover unpaid rent and charges and "
           "damage beyond normal wear and tear.")
    d.para("<b>Move-in funds due</b> (certified funds or approved electronic payment):")
    d.row([("First month / prorated rent $", "mi_rent", 80), ("+ deposits $", "mi_dep", 80),
           ("+ fees $", "mi_fees", None)])
    d.row([("= TOTAL DUE AT MOVE-IN: $", "mi_total", 110), ("due by (date):", "mi_due", None)])
    d.row([("Refundable deposits will be held by:", None, None), ("[]", "dep_held", "Landlord/Owner"),
           ("[]", "dep_held", "Property Manager")])

    # 8 screening
    d.section("8. Application and Screening")
    d.para("This Lease depends on Landlord’s satisfactory review of each adult Tenant’s application, "
           "including employment, income, credit, rental history, and criminal background. Tenant "
           "confirms the application is true and complete. A material false statement on the "
           "application allows Landlord to end this Lease and seek all lawful remedies. Tenant’s "
           "payment history under this Lease may be reported to credit bureaus.")

    # 9 utilities
    d.section("9. Utilities and Services")
    d.para("Check who pays each item. The responsible party must set up service in its own name by "
           "the start date and keep it on through the move-out inspection.")
    for left, right in [("Electricity", "Gas"), ("Water", "Sewer"), ("Trash / recycling", "Internet / cable"),
                        ("Landscaping service", "Other:")]:
        d.ensure(16)
        y = d.y - 13
        for j, label in enumerate((left, right)):
            x = LM + j * 260
            c.setFont("Helvetica", 9)
            c.drawString(x, y + 3, label)
            if label == "Other:":
                d.text_field(d.name("util_other"), x + 32, y, 80, 13)
            bx = x + 120
            for k, who in enumerate(("Landlord", "Tenant")):
                d.checkbox(d.name("util"), bx + k * 62, y + 1.5)
                c.drawString(bx + k * 62 + 12, y + 3, who)
        d.y -= 18

    # 10 pets
    d.section("10. Pets")
    d.para("“Pets” include any animal, bird, fish, or reptile. <b>Assistance animals</b> (service and "
           "support animals) are not pets, and no pet deposit or pet fee applies to them; requests are "
           "handled under fair housing law.")
    d.row([("[]", "pets", "No pets allowed without Landlord’s prior written consent.")])
    d.row([("[]", "pets", "Pets allowed (type, breed, weight, name):"), ("", "pets_desc", None)])
    d.para("Tenant is responsible for all pet damage, waste cleanup, and noise, and must keep pets "
           "leashed or contained outside the home. Unauthorized pets are a material noncompliance.")

    # 11 smoking & vehicles
    d.section("11. Smoking, Vehicles, and Parking")
    d.para("<b>Smoking:</b> Smoking or vaping of any substance (including tobacco and marijuana) is "
           "<b>prohibited inside the home and garage</b>. Tenant is responsible for smoke odor and damage.")
    d.row([("Maximum number of vehicles on the property:", "max_vehicles", 40)])
    d.para("All vehicles must be operable, currently registered, and parked only in the driveway, garage, "
           "or assigned spaces — never on landscaping or the yard. Inoperable, unregistered, or "
           "abandoned vehicles, boats, trailers, and RVs may not be stored on the property without "
           "written consent. See also Section 25 (Additional Terms).")

    # 12 keys
    d.section("12. Keys and Locks")
    d.row([("Door keys:", "keys_door", 34), ("Garage openers:", "keys_garage", 34),
           ("Mailbox keys:", "keys_mail", 34), ("Gate/other:", "keys_other", None)])
    d.row([("Locks were re-keyed after the previous tenant:", None, None), ("[]", "rekey", "Yes"),
           ("[]", "rekey", "No")])
    d.para("Tenant may not change or add locks without Landlord’s written consent and must give "
           "Landlord a copy of any approved new key. Tenant pays to replace lost or unreturned keys and "
           "openers. Tenant remains responsible for rent and the Premises until all keys and openers "
           "are returned to Landlord (leaving them inside the home is not a return unless Landlord "
           "agrees in writing).")

    # 13 maintenance
    d.section("13. Maintenance and Repairs")
    d.para("<b>Landlord</b> will keep the Premises fit and habitable, comply with building and housing "
           "codes, make all necessary repairs, and keep supplied appliances and the electrical, "
           "plumbing, heating, cooling, and other systems in good working order, as required by the "
           "ARLTA.")
    d.para("<b>Tenant</b> will keep the Premises clean and safe; use all fixtures and appliances "
           "reasonably; dispose of trash properly; replace air filters, light bulbs, and smoke/CO "
           "detector batteries as needed; and not damage the Premises or allow others to. Tenant must "
           "<b>promptly notify Landlord in writing</b> of any needed repair, leak, moisture, or mold. "
           "Damage caused by Tenant, occupants, guests, or pets beyond normal wear and tear will be "
           "repaired at Tenant’s expense. A written repair request is Tenant’s permission for "
           "Landlord to enter to make that repair.")
    d.para("Responsibility for the following items:")
    for label in ("Front yard", "Back yard", "Pool / spa", "Routine pest control"):
        d.row([(label + ":", None, 130), ("[]", "maint", "Landlord"), ("[]", "maint", "Tenant"),
               ("[]", "maint", "HOA"), ("[]", "maint", "Not applicable")], after=3)
    d.space(2)

    # 14 safety
    d.section("14. Safety Devices and Property Disclosures")
    d.row([("Smoke detectors:", None, None), ("[]", "smoke", "Installed"), ("[]", "smoke", "Not installed"),
           ("   Carbon monoxide detectors:", None, None), ("[]", "co", "Installed"),
           ("[]", "co", "Not installed / no gas")])
    d.row([("Fire sprinklers:", None, None), ("[]", "spr", "Yes"), ("[]", "spr", "No"),
           ("   Swimming pool or spa:", None, None), ("[]", "pool", "Yes"), ("[]", "pool", "No")])
    d.para("Tenant must keep detectors working, test them monthly, replace batteries, and report any "
           "missing or broken device to Landlord right away. Tenant must not disable any safety "
           "device. If there is a pool or spa, Tenant acknowledges receiving the Arizona private pool "
           "safety notice and agrees to follow all pool barrier laws and keep gates closed and latched.")
    d.row([("Community association (HOA):", None, None), ("[]", "hoa", "No"),
           ("[]", "hoa", "Yes — name:"), ("", "hoa_name", None)])
    d.para("If yes, Tenant received a copy of the HOA rules and must follow them. Tenant pays any HOA "
           "fine caused by Tenant, occupants, guests, or pets. Regular HOA dues are paid by Landlord.")
    d.para("<b>Lead-based paint</b> (check one):")
    d.row([("[]", "lbp", "The home was built in 1978 or later.")], after=3)
    d.row([("[]", "lbp", "The home was built before 1978. Tenant received the federal Lead-Based Paint "
            "Disclosure and the EPA pamphlet.")])
    d.initials_line()

    # 15 bed bugs
    d.section("15. Bed Bugs (A.R.S. § 33-1319)")
    d.para("Landlord will not knowingly rent a unit with a current bed bug infestation. Tenant "
           "received the bed bug information in <b>Attachment A</b>. Tenant must promptly report any "
           "suspected bed bugs in writing, must not knowingly bring infested items into the home, "
           "and must cooperate with inspection and treatment.")
    d.initials_line()

    # 16 entry
    d.section("16. Landlord Entry and Inspections (A.R.S. § 33-1343)")
    d.para("Tenant will not unreasonably refuse entry. Landlord may enter at reasonable times, after "
           "giving at least <b>two (2) days’ notice</b>, to inspect, make repairs or improvements, "
           "supply services, or show the Premises to buyers, lenders, tenants, or contractors. "
           "Landlord may enter without notice in an emergency. Landlord will not abuse the right of "
           "entry or use it to harass Tenant. Routine inspections are described in Section 25.")

    # 17 alterations
    d.section("17. Alterations")
    d.para("Tenant may not paint, wallpaper, install fixtures, satellite equipment, or make any "
           "alterations or improvements without Landlord’s prior written consent. Tenant will pay "
           "to restore the Premises from any unapproved change.")

    # 18 insurance
    d.section("18. Renter’s Insurance (Required)")
    d.row([("Tenant must carry renter’s insurance with personal liability coverage of at least $",
            "ins_min", None)])
    d.para("for the entire tenancy and give Landlord proof of coverage <b>before receiving keys</b> and "
           "at each renewal. Landlord should be listed as an “interested party” so the "
           "insurer notifies Landlord if the policy is cancelled. Landlord’s insurance does "
           "<b>not</b> cover Tenant’s belongings or Tenant’s liability. Except for losses "
           "caused by Landlord’s negligence, Tenant is responsible for injury, loss, or damage "
           "caused by Tenant, occupants, guests, or pets. Failure to keep insurance is a material "
           "noncompliance.")
    d.row([("Insurance company:", "ins_co", 230), ("Policy #:", "ins_policy", None)])

    # 19 conduct
    d.section("19. Conduct and Crime-Free Provision")
    d.para("Tenant, occupants, and guests must not disturb neighbors’ peaceful enjoyment or "
           "create a nuisance. They must not engage in or allow on or near the Premises: illegal "
           "drug activity; violence or threats of violence; unlawful discharge of a firearm; "
           "prostitution; gang activity; or any act that seriously damages property or endangers the "
           "health or safety of others. <b>Such conduct is a material and irreparable breach under "
           "A.R.S. § 33-1368(A)(2), allowing immediate termination of this Lease.</b>")
    d.initials_line()

    # 20 moving
    d.section("20. Move-In and Move-Out")
    d.para("Landlord gives Tenant the <b>Move-In / Move-Out Condition Form (Attachment B)</b> at "
           "move-in. Tenant must note any existing damage and return it signed within <b>five (5) "
           "days</b> after move-in; otherwise the Premises are accepted in the condition shown when "
           "the tenancy began. Tenant has the right to be present at the move-out inspection and "
           "should request the time in writing.")
    d.para("When moving out, Tenant must: leave the Premises clean and in the same condition as at "
           "move-in, except normal wear and tear; remove all belongings and trash; return all keys and "
           "openers; keep utilities on through the move-out inspection; and give Landlord a "
           "<b>forwarding address in writing</b> for the deposit refund.")

    # 21 emergency
    d.section("21. Emergency Contact and Death of Tenant")
    d.row([("Emergency contact:", "emerg_name", 170), ("Phone:", "emerg_phone", 100),
           ("Relationship:", "emerg_rel", None)])
    d.para("Tenant authorizes the person above to enter the Premises to remove and store Tenant’s "
           "belongings if Tenant dies during the tenancy. Otherwise, Landlord will handle belongings "
           "as allowed by the ARLTA.")

    # 22 legal
    d.section("22. Notices")
    d.para("Notices must be in writing and delivered in person or by registered or certified mail: "
           "to Landlord at the Property Manager (or Owner) address in Section 1, and to Tenant at the "
           "Premises. A mailed notice is considered received when actually received or five (5) days "
           "after mailing, whichever is first. Tenant may also send routine repair requests by "
           "email to the Property Manager.")

    d.section("23. Military Service")
    d.para("A Tenant who enters military service, or who receives orders for a permanent change of "
           "station or deployment of 90 days or more, may end this Lease by giving Landlord written "
           "notice and a copy of the orders, as allowed by A.R.S. § 33-1413 and the federal "
           "Servicemembers Civil Relief Act.")

    d.section("24. General Terms")
    for t in [
        "<b>Foreclosure:</b> Landlord will notify Tenant in writing within five (5) days after "
        "receiving any trustee’s sale or foreclosure notice on the Premises, as required by "
        "A.R.S. § 33-1331. Tenant will notify Landlord immediately of any such notice posted or "
        "delivered at the Premises.",
        "<b>Attorney fees:</b> In any dispute arising from this Lease, the prevailing party may "
        "recover reasonable attorney fees and costs.",
        "<b>Fair housing:</b> Landlord complies with all federal, state, and local fair housing laws.",
        "<b>Waiver:</b> Landlord’s failure to enforce a term is not a waiver of that term or of "
        "any later breach. Any waiver must be in writing.",
        "<b>Severability:</b> If any part of this Lease is invalid, the rest remains in effect, and "
        "the invalid part will be enforced to the extent the law allows.",
        "<b>Entire agreement:</b> This Lease and its attachments are the entire agreement. Changes "
        "must be in writing and signed by Landlord and Tenant. Each adult Tenant is jointly and "
        "severally (individually and together) responsible for all obligations.",
        "<b>Governing law:</b> Arizona law governs this Lease. The ARLTA controls over any term of "
        "this Lease that conflicts with it.",
        "<b>Electronic signatures and copies:</b> This Lease may be signed electronically and in "
        "counterparts; signed copies are treated as originals.",
        "<b>Time is of the essence</b> for all deadlines in this Lease.",
    ]:
        d.para(t, after=3)

    # 25 additional terms
    d.section("25. Additional Terms (Rules of the Property)")
    d.para("Breaking these rules is a noncompliance with this Lease. Except in an emergency, Landlord "
           "will give Tenant written notice and an opportunity to fix the problem as the ARLTA "
           "requires before taking further action.", after=5)
    rules = [
        "<b>Respect and quiet enjoyment.</b> Tenant, occupants, and guests will treat neighbors, "
        "Landlord, and Landlord’s agents and contractors with courtesy. No harassment, threats, "
        "or abusive language. Quiet hours are <b>10:00 p.m. to 7:00 a.m.</b>; at all times noise must "
        "not disturb neighbors.",
        "<b>Quarterly inspections.</b> Landlord may inspect the Premises <b>once every three (3) "
        "months</b> (and at move-in and move-out), with at least two (2) days’ notice as required "
        "by A.R.S. § 33-1343. Inspections check condition, safety devices, filters, leaks, and "
        "compliance with these rules.",
        "<b>Vehicles.</b> Only operable, currently registered vehicles may be kept on the property, "
        "within the vehicle limit in Section 11. No parking on landscaping, gravel, or the yard.",
        "<b>No vehicle or equipment repairs.</b> No repair, overhaul, painting, or fluid changes of "
        "vehicles, boats, or equipment on the property. Minor emergency work (jump-start, flat tire) "
        "is allowed if finished the same day. Tenant pays to clean any oil or fluid stains.",
        "<b>Storage room.</b> The storage room is for normal household items only. It may not be "
        "overfilled, used for business inventory, or used to store flammable or hazardous "
        "materials, and items must not block doors, the water heater, or electrical panels.",
        "<b>Yard and exterior kept clean.</b> The front yard, back yard, driveway, porch, and sides "
        "of the home must be kept free of trash, debris, junk, indoor furniture, appliances, and "
        "loose items. Trash goes in closed bins; bins are returned out of street view within 24 hours "
        "after collection day. If Tenant does not correct a violation after written notice, "
        "Landlord may have it cleaned and charge Tenant the reasonable cost as allowed by the ARLTA.",
        "<b>No business or hazardous use.</b> No business that brings customers or traffic to the "
        "Premises, and no storage of fuel, chemicals, or explosives beyond normal household amounts.",
    ]
    for i, r in enumerate(rules, 1):
        d.para(f"{i}.&nbsp; " + r, BULLET, after=3)
    d.para("Other terms agreed for this property:", after=3)
    d.multiline("addl_terms", lines=4)

    # 26 acknowledgment & signatures
    d.section("26. Acknowledgments and Signatures")
    d.para("By signing, each Tenant confirms that Tenant: (a) read and understands this entire Lease; "
           "(b) was told a free copy of the ARLTA is available from the Arizona Department of Housing; "
           "(c) received the Move-In / Move-Out Condition Form (Attachment B) and the bed bug "
           "information (Attachment A); (d) knows of the right to be present at the move-out "
           "inspection; and (e) received a complete signed copy of this Lease and all attachments.")
    d.row([("Attachments:", None, None), ("[]", "att", "A – Bed Bug Information"),
           ("[]", "att", "B – Move-In/Move-Out Form"), ("[]", "att", "Lead Paint Disclosure"),
           ("[]", "att", "HOA Rules")])
    d.row([("[]", "att", "Other:"), ("", "att_other", None)])
    d.space(4)
    for i in range(1, 5):
        d.signature_block(f"Tenant {i}", f"sig_t{i}")
    d.signature_block("Landlord / Authorized Agent of " + "Hidalgo Property Management LLC", "sig_l")


def attachment_a(d: Doc):
    d.new_page()
    d.c.setFont("Helvetica-Bold", 14)
    d.c.setFillColor(ACCENT)
    d.c.drawCentredString(PAGE_W / 2, d.y - 14, "ATTACHMENT A — BED BUG INFORMATION")
    d.c.setFillColor(black)
    d.y -= 30
    d.para("Arizona law (A.R.S. § 33-1319) requires landlords to give tenants educational "
           "information about bed bugs. Please read this page and keep it.", after=8)
    blocks = [
        ("What are bed bugs?", "Small, flat, reddish-brown insects about the size of an apple seed "
         "that feed on human blood, usually at night. They hide in mattress seams, box springs, bed "
         "frames, headboards, furniture, cracks, and behind outlet covers and baseboards. They do "
         "not mean a home is dirty — they travel easily in luggage, clothing, and used furniture."),
        ("Signs of bed bugs", "Itchy bites, often in a line or cluster; small rust-colored or dark "
         "spots on sheets or mattresses; shed skins or eggs in seams; live bugs; a musty odor."),
        ("How to prevent them", "Inspect used furniture, mattresses, and clothing before bringing "
         "them home — avoid furniture left on the curb. Check luggage and hotel beds when "
         "traveling; wash and dry travel clothes on high heat when you return. Reduce clutter, "
         "especially around beds. Consider a bed bug–proof mattress encasement."),
        ("If you think you have bed bugs", "Notify the Landlord/Property Manager in writing right "
         "away — early treatment is faster and cheaper. Do <b>not</b> use over-the-counter "
         "foggers or “bug bombs”: they scatter bed bugs into other rooms. Do not move "
         "furniture or belongings to another home until treated. Cooperate with inspection and "
         "treatment, and follow the pest professional’s preparation instructions."),
        ("Washing and drying", "Bed bugs and eggs die at high heat. Wash bedding and clothing in hot "
         "water and dry on the highest dryer setting for at least 30 minutes. Seal cleaned items in "
         "plastic bags until treatment is finished."),
        ("More information", "U.S. EPA: epa.gov/bedbugs  •  Arizona Department of Housing: "
         "housing.az.gov"),
    ]
    for t, b in blocks:
        d.para(f"<b>{t}</b>", after=1)
        d.para(b, after=7)
    d.space(6)
    d.para("I/We received and read this bed bug information.", after=2)
    d.signature_block("Tenant", "bb_t1")
    d.signature_block("Tenant", "bb_t2")


def attachment_b(d: Doc):
    d.new_page()
    c = d.c
    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(ACCENT)
    c.drawCentredString(PAGE_W / 2, d.y - 14, "ATTACHMENT B — MOVE-IN / MOVE-OUT CONDITION FORM")
    c.setFillColor(black)
    d.y -= 28
    d.row([("Premises:", "mf_addr", 300), ("Tenant(s):", "mf_tenant", None)])
    d.row([("Move-in inspection date:", "mf_in_date", 100), ("Move-out inspection date:", "mf_out_date", None)])
    d.para("Describe the condition of each item (e.g., clean, new, scratched, stained, chipped, "
           "missing, not working). Tenant must return this form within <b>five (5) days</b> after "
           "move-in. Photos/video are recommended for both inspections.", after=6)

    rooms = [
        ("Entry / Living Room", ["Walls & ceiling", "Floors / carpet", "Doors, locks & windows",
                                 "Blinds / screens", "Lights, outlets & fans"]),
        ("Kitchen", ["Walls, floors & cabinets", "Counters & sink", "Refrigerator", "Range / oven / hood",
                     "Dishwasher / microwave"]),
        ("Bedroom 1", ["Walls, ceiling & floors", "Closet, doors & windows"]),
        ("Bedroom 2", ["Walls, ceiling & floors", "Closet, doors & windows"]),
        ("Bedroom 3", ["Walls, ceiling & floors", "Closet, doors & windows"]),
        ("Bathroom(s)", ["Toilet, tub & shower", "Sink, vanity & mirror", "Floors, walls & fan"]),
        ("Laundry / Garage / Storage", ["Washer / dryer hookups", "Garage door & opener",
                                        "Storage room", "Water heater / A/C filter"]),
        ("Exterior", ["Front yard", "Back yard", "Driveway / patio", "Smoke / CO detectors tested"]),
    ]
    col_item, col_in, col_out = 140, (TEXT_W - 140) / 2, (TEXT_W - 140) / 2

    def header():
        d.ensure(30)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(LM, d.y - 10, "Item")
        c.drawString(LM + col_item, d.y - 10, "Condition at MOVE-IN")
        c.drawString(LM + col_item + col_in, d.y - 10, "Condition at MOVE-OUT")
        d.y -= 14

    header()
    for room, items in rooms:
        if d.y - (16 + 17 * len(items)) < BOTTOM:
            d.new_page()
            header()
        c.setFillColor(HexColor("#dfe6f0"))
        c.rect(LM, d.y - 13, TEXT_W, 13, stroke=0, fill=1)
        c.setFillColor(black)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(LM + 3, d.y - 10, room)
        d.y -= 16
        for it in items:
            y = d.y - 14
            c.setFont("Helvetica", 8.5)
            c.drawString(LM + 3, y + 3, it)
            d.text_field(d.name("mf_in"), LM + col_item, y, col_in - 6, 14, size=8)
            d.text_field(d.name("mf_out"), LM + col_item + col_in, y, col_out, 14, size=8)
            d.y -= 17
    d.space(4)
    d.para("<b>Keys received at move-in</b> (doors / garage openers / mailbox / other):", after=2)
    d.row([("", "mf_keys_in", None)])
    d.para("<b>Meter readings at move-in</b> (electric / gas / water), if applicable:", after=2)
    d.row([("", "mf_meters", None)])
    d.para("<b>Notes:</b>", after=2)
    d.multiline("mf_notes", lines=3)
    d.para("<b>Move-in:</b> Tenant and Landlord agree this form shows the condition at move-in.", after=0)
    d.signature_block("Tenant", "mf_in_t")
    d.signature_block("Landlord / Agent", "mf_in_l")
    d.para("<b>Move-out:</b> Signed at the move-out inspection. Tenant forwarding address for "
           "deposit refund:", after=2)
    d.row([("", "mf_fwd_addr", None)])
    d.signature_block("Tenant", "mf_out_t")
    d.signature_block("Landlord / Agent", "mf_out_l")


def build_lease(path):
    def render(stream, total):
        d = Doc(stream, "Residential Lease Agreement (Arizona)", total_pages=total)
        lease(d)
        attachment_a(d)
        attachment_b(d)
        d.finish()
        return d.page

    pages = render(io.BytesIO(), None)
    with open(path, "wb") as f:
        render(f, pages)
    return pages


# =====================================================================
#  LANDLORD CHECKLIST (internal — do not give to tenant)
# =====================================================================

def build_checklist(path):
    def render(stream, total):
        d = Doc(stream, "Landlord New-Tenant Checklist (internal)", total_pages=total, footer=False)
        c = d.c
        c.setFont("Helvetica-Bold", 15)
        c.setFillColor(ACCENT)
        c.drawCentredString(PAGE_W / 2, d.y - 14, "NEW TENANT CHECKLIST — LANDLORD USE ONLY")
        c.setFillColor(black)
        d.y -= 26
        d.para("Internal tool for " + COMPANY + ". Do not give this page to the tenant. Check each "
               "item off as you complete the lease.", BODY_SMALL, after=6)
        d.row([("Property:", "ck_prop", 300), ("Tenant(s):", "ck_ten", None)])

        groups = [
            ("Before signing", [
                "Application, ID, income, credit, rental-history, and background checks done for every adult (18+).",
                "Screening criteria applied the same way to every applicant (fair housing).",
                "Assistance-animal requests handled separately — no pet fee or pet deposit charged for them.",
                "Owner name and address filled in (Section 1 — required by A.R.S. § 33-1322).",
                "Property Manager address, phone, and email filled in.",
                "Every adult listed as a Tenant; all other occupants (including children) listed in Section 3.",
            ]),
            ("Money", [
                "Rent amount and due day filled in; prorated first month calculated if needed.",
                "Late fee and returned-payment fee filled in and reasonable.",
                "Refundable deposits + prepaid rent are no more than 1½ months’ rent.",
                "Every non-refundable fee is written in Section 7 as non-refundable.",
                "No rental sales tax added (residential rental tax ended Jan 1, 2025).",
                "Move-in total adds up; funds collected as certified funds or cleared electronic payment.",
            ]),
            ("Property-specific sections — verify each time", [
                "Appliances included (Section 2) and parking spaces.",
                "Utilities: who pays each one (Section 9) — never leave blank.",
                "Pets: allowed or not; description of approved pets.",
                "Vehicle limit (Section 11) and number of keys/openers (Section 12); locks re-keyed.",
                "Yard, pool, and pest-control responsibility (Section 13).",
                "Smoke/CO detectors, sprinklers, pool, HOA, and year built (Section 14) match THIS home.",
                "Smoke detectors tested; CO detector installed if the home has gas appliances or an attached garage.",
                "If built before 1978: federal Lead-Based Paint Disclosure signed + EPA pamphlet given.",
                "If pool/spa: Arizona private pool safety notice given.",
                "If HOA: copy of HOA rules given.",
                "Renter’s insurance minimum filled in (Section 18; $100,000 liability suggested).",
                "Any special terms written in Section 25 — specific and measurable, not vague.",
            ]),
            ("At signing", [
                "Every adult tenant initials every page and signs Section 26.",
                "Initials at Sections 14 (lead paint), 15 (bed bugs), and 19 (crime-free).",
                "Landlord/agent signs and dates.",
                "Attachment A (bed bugs) signed; tenant told ARLTA copy is available at housing.az.gov.",
                "Complete signed copy of the lease and all attachments given to every tenant.",
            ]),
            ("Before handing over keys", [
                "Proof of renter’s insurance received; landlord listed as interested party.",
                "Utilities transferred into tenant’s name (per Section 9).",
                "Attachment B (Move-In form) given; photos/video of the home taken and dated.",
                "Calendar: collect Attachment B within 5 days; first quarterly inspection in 3 months.",
            ]),
            ("At move-out", [
                "Tenant asked in writing whether they want to attend the move-out inspection.",
                "Move-out inspection done; Attachment B completed; photos taken.",
                "Forwarding address received in writing.",
                "Deposit refund + itemized deductions sent within 14 business days (A.R.S. § 33-1321).",
            ]),
        ]
        for title, items in groups:
            d.section(title)
            for it in items:
                p = Paragraph(it, BODY)
                w, h = p.wrap(TEXT_W - 16, 500)
                d.ensure(h + 3)
                d.checkbox(d.name("ck"), LM, d.y - 10, size=9)
                p.drawOn(c, LM + 16, d.y - h)
                d.y -= max(h, 11) + 4
        d.space(6)
        d.para("<i>This form and the lease are general templates based on the Arizona Residential "
               "Landlord and Tenant Act. Laws change — have an Arizona landlord-tenant attorney "
               "review the template periodically.</i>", BODY_SMALL)
        d.finish()
        return d.page

    pages = render(io.BytesIO(), None)
    with open(path, "wb") as f:
        render(f, pages)
    return pages


if __name__ == "__main__":
    lp = os.path.join(OUT_DIR, "Hidalgo_Residential_Lease.pdf")
    cp = os.path.join(OUT_DIR, "Landlord_New_Tenant_Checklist.pdf")
    print("lease pages:", build_lease(lp))
    print("checklist pages:", build_checklist(cp))
