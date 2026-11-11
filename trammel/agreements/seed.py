"""Starting data for the agreements module (see ModuleManifest.seed)."""

from __future__ import annotations

DEFAULT_BODY = """\
TERMS AND CONDITIONS OF SERVICE

Job: {{ job.number }} — {{ job.title }}
Shop: {{ business.legal_name }}
Customer: {{ customer.name }}{% if customer.company %}, of {{ customer.company }}{% endif %}

Issued: {{ today|date }}

These Terms and Conditions ("Terms") govern the work performed by {{ business.legal_name }} ("the Shop") for {{ customer.name }}{% if customer.company %}, of {{ customer.company }}{% endif %} ("the Customer") in connection with the job identified above ("the Job").

These Terms, together with the approved quotation, applicable drawings, and specifications, and any approved change orders, constitute the agreement between the parties ("the Agreement").

By approving a quotation, accepting these Terms electronically or in writing, or otherwise expressly authorizing the Shop to proceed under the Agreement, the Customer agrees to be bound by these Terms.

1. QUOTATIONS AND ESTIMATES

1.1. Quotations are based on the information, drawings, specifications, quantities, materials, and requirements available when prepared.

1.2. Unless otherwise stated, quotations remain valid for 30 calendar days from the date of issuance. The Shop reserves the right to revise expired quotations to reflect changes in material costs, availability, labor requirements, or other relevant circumstances.

1.3. A quotation does not guarantee production capacity, material availability, or a specific completion date until accepted by the Customer and confirmed by the Shop.

1.4. Quoted prices apply only to the work expressly described. Services, features, materials, or requirements not included in the approved quotation are excluded unless subsequently authorized through a change order.

1.5. Estimates are approximate and may be adjusted when actual conditions or requirements become known. Additional charges beyond the approved scope or authorized allowances require Customer approval through a change order.

1.6. The Shop may correct obvious clerical, mathematical, or typographical errors before quotation acceptance. Corrections after acceptance require agreement between the parties. Uncorrected errors may be cause for termination of the quotation.

1.7. Unless expressly included, quotations exclude applicable taxes, delivery, installation, permits, engineering certification, specialized testing, and other services not specifically identified.

1.8. Rush jobs may be accepted, as determined by the Shop, if disclosed prior to quotation. Rush jobs are subject to a rush fee, determined by the Shop, and included in the quotation. Jobs to be elevated to rush status by the Customer must have an approved change order including the rush fee and revised dates. All rush jobs are subject to the maximum deposit allowed by law.

2. SCOPE OF WORK

2.1. The Shop will perform the work described in the approved quotation and associated specifications, as modified by any subsequently approved change orders.

2.2. The approved scope establishes the agreed deliverables, materials, quantities, and requirements. Preliminary discussions, conceptual drawings, examples, and informal communications do not expand the scope unless expressly incorporated into the Agreement.

2.3. The Shop may select appropriate manufacturing methods, tools, equipment, and production processes, provided the finished work conforms to the agreed specifications. The Customer may only specify the manufacturing method if included in the agreed specifications.

2.4. Work not expressly included in the approved scope is considered additional work and requires authorization through a change order.

2.5. The Shop is not required to perform work that is unsafe, unlawful, technically impractical, or outside its reasonable capabilities. Illegal, immoral, and unethical work will not be performed. Work found to be in violation will result in deposit and material forfeiture as allowed by law.

2.6. The Shop may propose alternative methods, materials, or designs when necessary or advantageous. Any alternative materially affecting the agreed specifications, scope, cost, or schedule requires Customer approval through a change order before implementation. Alternatives not materially affecting the specifications, scope, cost, or schedule may be implemented by the Shop with no notice to the Customer.

3. CHANGE ORDERS

3.1. Any modification to the approved scope of work, including changes to designs, specifications, materials, quantities, finishes, deliverables, or other requirements, shall be documented through a change order.

3.2. Change orders may be initiated by either the Customer or the Shop and shall identify the proposed modifications, associated price adjustments, and any anticipated effect on the completion schedule.

3.3. Change orders require approval by both parties before the affected work begins. Approval may be provided electronically or through another documented written method accepted by the Shop.

3.4. The Shop is not obligated to perform additional or modified work without an approved change order. Work affected by a pending change order may be suspended until approval is received.

3.5. Approved change orders become part of the Agreement and modify the original quotation and scope of work only to the extent expressly stated. All unaffected terms remain in effect.

3.6. If unforeseen conditions make the approved scope impractical, unsafe, or impossible to complete as specified, the Shop will notify the Customer and propose an appropriate change order. If agreement cannot be reached, the affected work may be suspended or terminated in accordance with the cancellation provisions of these Terms.

3.7. Verbal requests, informal discussions, and the absence of an objection do not constitute authorization for additional billable work.

3.8. If a change order expressly includes amended contractual terms, those amendments apply only to the extent approved by both parties.

4. CUSTOMER RESPONSIBILITIES

4.1. The Customer is responsible for providing accurate and complete information necessary to perform the Job, including dimensions, drawings, specifications, intended use, material requirements, and any applicable performance criteria. Changes to any information after acceptance may require a change order.

4.2. The Customer shall review and respond to requests for approvals, clarifications, proofs, and change orders within a reasonable time (5 business days). Failure to respond will result in the Job being placed on hold with missed deadline consequences waived.

4.3. The Customer shall disclose any unusual operating conditions, safety requirements, environmental exposure, regulatory restrictions, or specialized performance expectations before approving the Job. Disclosure of adverse information after acceptance may result in a change order to incorporate or termination of the Job.

4.4. The Customer represents that it has the necessary rights and permissions to provide any drawings, artwork, logos, trademarks, designs, or other materials submitted for use in the Job. The Shop will not be held liable for Customer errors or omissions.

4.5. Unless the Shop has expressly agreed to provide engineering or design verification services, the Customer is responsible for determining whether the requested product or design is suitable for its intended use. The Shop does not provide engineering services or make guarantees as to the suitability of products or designs.

4.6. The Customer is responsible for identifying critical dimensions, tolerances, fitment requirements, and interfaces with existing equipment or components. Changes to, or disclosure of, any information after acceptance may require a change order.

4.7. Delays resulting from incomplete information, unavailable Customer-supplied materials, or pending Customer approvals may extend the production schedule or result in termination of the Job.

5. DESIGNS, DRAWINGS, AND APPROVALS

5.1. Approval of a drawing, proof, model, sample, or design authorizes the Shop to proceed according to the approved version.

5.2. The Customer is responsible for reviewing spelling, dimensions, layout, orientation, material selection, finishes, and other relevant details before approval.

5.3. The Shop is not responsible for errors contained in Customer-provided or Customer-approved designs, except where the Shop expressly assumes responsibility for the affected design work or fails to manufacture according to the approved specifications.

5.4. Drawings, renderings, digital proofs, and photographs may not precisely represent the appearance of finished materials or products. Variations arising from lighting, display settings, manufacturing methods, and material characteristics should be expected.

5.5. Changes requested after approval that materially affect the scope, price, specifications, or schedule require an approved change order.

5.6. Unless expressly agreed in writing, the Shop does not provide professional engineering services, structural certification, regulatory compliance verification, or independent validation of Customer-supplied designs.

5.7. Approval may be provided electronically or through another documented written method accepted by the Shop.

5.8. Unless otherwise specified, all drawings, proofs, models, samples, designs, renderings, photographs, and other assets produced for the Job remain property of the Shop. These are not considered "works for hire" unless identified in the specifications as such.

6. MATERIALS AND MANUFACTURING VARIATIONS

6.1. Natural materials, including wood, leather, stone, and similar products, may exhibit variations in color, grain, texture, density, character, and appearance.

6.2. Manufactured materials may exhibit normal variations in thickness, flatness, surface condition, color, and other characteristics within commercially accepted tolerances.

6.3. Manufacturing processes such as cutting, machining, bending, forming, welding, engraving, finishing, and assembly may produce minor dimensional or cosmetic variations.

6.4. Unless expressly specified, the Shop will apply commercially reasonable workmanship standards appropriate to the material, manufacturing process, and intended application.

6.5. Specific dimensional tolerances, surface finishes, cosmetic standards, inspection methods, and acceptance criteria must be identified and agreed upon before production. Changes to any acceptance criteria may result in a change order.

6.6. General-purpose fabrication work is not presumed to require precision machining tolerances, cosmetic perfection, or specialized quality-control procedures unless expressly specified.

6.7. Normal manufacturing characteristics, including minor tool marks, weld discoloration, forming marks, grain variations, or other process-related features, do not constitute defects when consistent with the agreed specifications and reasonable workmanship standards.

6.8. Materials may exhibit movement, expansion, contraction, warping, oxidation, corrosion, or other changes resulting from environmental conditions or their inherent physical properties.

6.9. The Shop will not make material substitutions that materially affect appearance, performance, specifications, or price without Customer approval through a change order.

6.10. If material defects, availability issues, or unexpected conditions materially affect the approved scope, cost, or schedule, the Shop will notify the Customer and obtain approval of an appropriate change order before proceeding with the affected additional work.

7. CUSTOMER-SUPPLIED MATERIALS AND PROPERTY

7.1. The Shop may accept Customer-supplied materials, components, equipment, or property for fabrication, modification, repair, or incorporation into the Job. The Shop reserves the exclusive right to decline the use of customer materials.

7.2. Customer-supplied materials are provided with the understanding that their composition, condition, suitability, and manufacturing history may not be known to the Shop.

7.3. The Customer is responsible for disclosing known defects, hazardous substances, unusual properties, previous repairs, and other conditions that may affect safe handling or processing.

7.4. The Shop may refuse to work with materials that appear unsafe, unsuitable, contaminated, improperly identified, or incompatible with available equipment or processes.

7.5. Fabrication, machining, disassembly, repair, and modification may involve irreversible changes and inherent risks, including breakage, deformation, discoloration, loss of material, or unsuccessful processing.

7.6. The Shop will exercise reasonable care when handling Customer property but does not guarantee successful processing of defective, fragile, previously damaged, or unpredictable materials.

7.7. The Shop remains responsible for damage caused by its negligence or failure to exercise reasonable care, subject to applicable law.

7.8. The Customer shall provide sufficient material to account for normal manufacturing waste, testing, setup, and reasonable anticipated losses unless otherwise agreed. Typically, a minimum of 10% of additional material is required.

7.9. If unexpected conditions or material deficiencies require additional work or replacement materials, the Shop will notify the Customer and obtain approval through a change order before proceeding beyond the authorized scope.

7.10. Unused Customer-supplied materials will be returned upon request, subject to lawful retention rights and outstanding charges. Ordinary scraps, offcuts, and manufacturing waste may be discarded unless the Customer requests their return before production begins. Such requests must be included within the Job specifications.

7.11. The Customer is responsible for identifying materials having unusual replacement value, sentimental value, or irreplaceable characteristics before work begins so that the parties may agree upon appropriate handling requirements.

7.12. The Customer must provide all materials prior to the Job being scheduled. Jobs without material will be placed on hold and terminated if not provided within a reasonable time (5 business days).

8. PRICING, DEPOSITS, AND PAYMENT

8.1. The total Job price consists of the approved quotation, adjusted by any approved change orders, applicable taxes, and other authorized charges.

{% if deposit_percent %}8.2. A deposit of {{ deposit_percent }}% of the approved quoted amount is due upon acceptance unless otherwise agreed in writing. The Shop may defer material purchases, scheduling, and production until the required deposit has been received and cleared.
{% else %}8.2. Any required deposit will be identified in the approved quotation. The Shop may defer material purchases, scheduling, and production until the required deposit has been received and cleared.
{% endif %}

8.3. Deposits are applied toward the total Job price. Deposits are not automatically nonrefundable and will be accounted for in accordance with the cancellation provisions of these Terms and applicable law.

8.4. The Shop may require progress payments for substantial, extended, or material-intensive Jobs when identified in the approved quotation or an approved change order.

8.5. Approved change orders may adjust the total Job price and establish additional deposit or progress payment requirements.

8.6. Unless otherwise specified in the approved quotation or an approved change order, invoices are due within {{ payment_terms_days }} calendar days of the invoice date.

8.7. The Shop may require payment in full before releasing or shipping finished work when disclosed in the approved quotation or otherwise agreed upon.

8.8. The Customer is responsible for applicable taxes, shipping charges, delivery costs, tariffs or duties, and other expenses identified in the quotation or subsequently authorized.

8.9. If an invoice remains unpaid after its due date, the Shop may suspend further work, withhold undelivered goods to the extent permitted by law, or decline additional work until the account is brought current.

8.10. Undisputed overdue balances may accrue simple interest at 1% per month, or the maximum rate permitted by applicable law, whichever is less, where such charges are enforceable.

8.11. The Customer shall promptly notify the Shop of any good-faith invoice dispute and identify the disputed amount and reason. Undisputed amounts remain payable when due.

8.12. The Shop may recover reasonable and documented collection expenses, returned-payment charges, and attorney fees only to the extent permitted by applicable law or an enforceable agreement.

8.13. Payments will be applied to outstanding charges in accordance with the applicable invoice and payment terms.

9. PRODUCTION SCHEDULING AND DELAYS

9.1. Estimated completion dates are provided in good faith but are not guaranteed unless expressly agreed in writing.

9.2. Production schedules may depend on material availability, equipment capacity, subcontractor lead times, Customer approvals, and other factors.

9.3. The Shop will make commercially reasonable efforts to complete the Job within the estimated schedule and will communicate material delays when reasonably practicable.

9.4. Customer-requested changes, delayed payments, unavailable materials, and delayed approvals may extend completion dates.

9.5. Schedule changes resulting from approved modifications will be documented in the applicable change order when reasonably ascertainable.

9.6. The Shop may suspend affected work while awaiting a required change order or other authorization. Unaffected work may continue where practical.

9.7. If circumstances substantially prevent timely completion, the parties will discuss a reasonable revised schedule or other appropriate resolution.

9.8. The Shop is not responsible for delays caused by circumstances beyond its reasonable control, subject to Section 19 and applicable law.

10. SUBCONTRACTING AND OUTSIDE SERVICES

10.1. The Shop may utilize qualified subcontractors or outside service providers for specialized processes, including machining, coating, plating, heat treatment, finishing, cutting, or any other production operations.

10.2. Unless otherwise disclosed and agreed, the Shop remains responsible for ensuring that subcontracted work conforms to the applicable agreed specifications.

10.3. Outside services may affect lead times and manufacturing tolerances.

10.4. Subcontracting that materially changes the approved scope, specifications, confidentiality requirements, price, or schedule requires Customer approval through a change order.

10.5. The Shop will not pass through unapproved additional subcontractor charges as Customer obligations.

11. INSPECTION AND ACCEPTANCE

11.1. The Shop will notify the Customer when completed work is available for inspection, pickup, or delivery.

11.2. The Customer is encouraged to inspect completed work promptly for apparent defects, missing items, shipping damage, and discrepancies from the agreed specifications.

11.3. The Customer should report reasonably discoverable discrepancies within 10 calendar days following receipt, providing photographs or supporting information when practical.

11.4. Failure to report an apparent defect within this period does not eliminate applicable warranty rights, statutory rights, or claims involving latent defects.

11.5. The Shop shall have a reasonable opportunity to inspect and correct verified nonconforming work before repairs or replacement are arranged through another provider, except where immediate action is reasonably necessary to prevent harm or additional damage.

11.6. Work that conforms to the approved specifications, including approved change orders and applicable manufacturing tolerances, is considered conforming work.

11.7. Customer-requested modifications beyond correction of a defect or nonconformity require a separate quotation or approved change order.

11.8. Any applicable statutory acceptance, rejection, or revocation rights remain unaffected.

12. COMPLETION, PICKUP, DELIVERY, AND STORAGE

12.1. Unless otherwise agreed, completed work will be made available for pickup at the Shop's designated location.

12.2. The Customer is responsible for arranging suitable transportation, loading assistance, and securing the finished work unless those services are expressly included.

12.3. The Shop will exercise reasonable care when assisting with loading but is not responsible for damage resulting from improper transportation, handling, or securing by the Customer or its agents.

12.4. Shipping methods, packaging, insurance, and associated charges will be established in the approved quotation or otherwise agreed upon.

12.5. Changes to delivery methods, destinations, installation requirements, or other delivery-related services that materially affect cost or scope require approval through a change order.

12.6. Risk of loss during shipment or delivery will be governed by the agreed delivery terms and applicable law. The Shop remains responsible only for its negligence, improper packaging, and failure to follow agreed shipping instructions.

12.7. The Customer should promptly report shipping damage and preserve relevant packaging and documentation to assist with carrier claims.

12.8. Completed work should be collected within 30 calendar days after notice of completion unless other arrangements have been approved.

12.9. If completed work remains uncollected after 30 days, the Shop may provide written notice that reasonable storage charges will begin after an additional 10 calendar days. The applicable charges must be disclosed before they accrue.

12.10. The Shop may require payment of lawful outstanding charges, including properly authorized storage charges, before releasing completed work.

12.11. Work remaining uncollected for an extended period may be subject to applicable abandoned-property, lien, or other legal procedures. The Shop will comply with all required notices, waiting periods, and procedures before selling, disposing of, or otherwise transferring Customer property.

13. CANCELLATION AND SUSPENSION

13.1. The Customer may request cancellation of the Job by providing written notice to the Shop.

13.2. If cancellation occurs before work begins and before noncancelable expenses are incurred, amounts paid will be refunded, less any agreed and legally permissible charges for services actually performed or expenses properly incurred.

13.3. If cancellation occurs after work begins, the Customer is responsible, to the extent permitted by law, for:

- Labor and services reasonably performed through the effective cancellation date.
- Materials purchased specifically for the Job.
- Noncancelable subcontractor commitments and third-party expenses.
- Documented cancellation or restocking charges actually incurred.
- Work and expenses properly authorized through approved change orders.
- Other amounts recoverable under applicable law.

13.4. The Shop will make reasonable efforts to minimize avoidable cancellation costs.

13.5. Deposits and other payments will be applied against amounts properly owed. Any remaining credit will be refunded, and any lawful outstanding balance will remain payable.

13.6. Custom or partially completed work may have limited resale or alternative use, which may affect the amounts reasonably recoverable following cancellation.

13.7. The Shop may suspend or terminate work upon reasonable notice for nonpayment, material breach of the Agreement, persistent failure to provide required approvals, unsafe conditions, or unlawful requests.

13.8. If an unforeseen condition requires a change order and the parties cannot agree upon the necessary modification, either party may request termination of the affected work, subject to an equitable accounting of completed work, committed expenses, and payments.

13.9. If the Shop terminates the Job for reasons unrelated to Customer breach, the Customer will not be charged for unperformed work, and the parties will reasonably account for completed work, materials, and payments.

13.10. Cancellation does not extinguish payment obligations or other rights that accrued before termination.

14. WORKMANSHIP WARRANTY

14.1. The Shop warrants that its workmanship will be free from defects resulting from improper fabrication, assembly, or installation performed by the Shop for a period of 90 calendar days following delivery or Customer pickup.

14.2. The warranty applies to defects attributable to the Shop's workmanship under normal use consistent with the disclosed intended purpose and agreed specifications.

14.3. The warranty does not cover:

- Normal wear, aging, or cosmetic changes associated with use.
- Misuse, abuse, accidents, neglect, or improper maintenance.
- Improper storage, handling, or installation by others.
- Modifications, alterations, or repairs performed by others.
- Natural material movement, checking, warping, expansion, contraction, or color changes.
- Corrosion, chemical exposure, weather damage, or environmental conditions beyond those specified.
- Defects inherent in Customer-supplied materials or components that were not caused by the Shop.
- Deficiencies in Customer-supplied or Customer-approved designs.
- Damage resulting from use outside the agreed operating conditions.

14.4. If a covered defect is confirmed, the Shop will, at its expense and within a reasonable time, repair the affected work, replace the defective portion, or provide an appropriate refund if repair or replacement is impracticable.

14.5. The Customer must provide reasonable access to the affected product for inspection and warranty service.

14.6. The Shop will cover reasonable transportation expenses necessary to remedy a verified warranty defect where required by applicable law or agreed in writing.

14.7. Third-party components may carry separate manufacturer warranties, which will be passed through to the Customer when transferable.

14.8. Warranty repairs do not extend the original warranty period unless required by law or expressly agreed in writing.

14.9. Customer-requested alterations or enhancements unrelated to correcting a covered defect require a separate quotation or approved change order.

14.10. This express workmanship warranty is supplemental to any mandatory statutory warranties or other rights that cannot legally be excluded or limited.

15. DESIGN RESPONSIBILITY AND INTENDED USE

15.1. Unless expressly contracted to provide design or engineering services, the Shop acts as a fabricator or service provider and relies on specifications supplied or approved by the Customer. "Parts built to customer's prints or specifications. "

15.2. The Customer is responsible for determining whether Customer-designed products are suitable for their intended use, including structural capacity, mechanical performance, safety, regulatory compliance, and compatibility with other systems.

15.3. The Shop may identify apparent design concerns or manufacturing limitations but has no general obligation to independently verify engineering calculations or discover all potential defects in Customer-supplied designs.

15.4. The Shop may decline work that reasonably appears unsafe, unlawful, technically impractical, or incompatible with its equipment or qualifications. The Shop does not perform illegal, immoral, or unethical work.

15.5. Products intended for life-safety, lifting, load-bearing, pressure-containing, electrical, medical, automotive safety, power generation, or other critical applications must be specifically identified and accepted in writing before work begins. The Shop does not produce, modify, or otherwise work on products or designs for life-safety, lifting, load-bearing, pressure-containing, electrical, medical, automotive safety, power generation, or other critical applications.

15.6. The Shop's manufacture or delivery of a product does not imply professional engineering approval, regulatory certification, or a particular safety rating.

15.7. Changes to the disclosed intended application or performance requirements that affect the approved scope must be documented through a change order.

15.8. Nothing in this section relieves the Shop of responsibility for its own negligent conduct, expressly assumed design obligations, or other nonwaivable legal duties.

16. INTELLECTUAL PROPERTY AND CUSTOMER DESIGNS

16.1. The Customer retains ownership of intellectual property, drawings, artwork, trademarks, and other proprietary materials supplied by the Customer.

16.2. The Customer grants the Shop a limited license to use such materials as reasonably necessary to quote, manufacture, inspect, deliver, and support the Job.

16.3. Unless otherwise agreed in writing, the Shop retains ownership of its preexisting designs, methods, templates, fixtures, tooling concepts, software, manufacturing processes, and other independently developed intellectual property.

16.4. Payment for fabrication does not automatically transfer ownership of the Shop's preexisting intellectual property.

16.5. Ownership and permitted reuse of custom design deliverables specifically commissioned and paid for by the Customer will be determined by the approved quotation, an approved change order, or a separate written agreement. In the absence of a specific transfer agreement, the Customer receives a nonexclusive license to use delivered design materials for the purpose for which they were commissioned, subject to applicable law.

16.6. The Shop will not knowingly reproduce Customer-provided confidential or proprietary designs for unrelated customers without authorization.

16.7. The Customer represents that it has the necessary rights to authorize the Shop's use of submitted materials.

16.8. The Customer is responsible for third-party intellectual property claims arising from Customer-supplied designs or instructions to the extent caused by those materials and permitted by law, excluding claims caused by the Shop's unauthorized modifications or independent conduct.

17. TOOLING, FIXTURES, AND PRODUCTION FILES

17.1. Unless expressly specified in the approved quotation or a change order, setup, programming, tooling, and fixture charges compensate the Shop for production preparation and do not constitute a purchase of the resulting tools or files.

17.2. Shop-created jigs, fixtures, machine programs, setup sheets, templates, and process documentation remain the property of the Shop unless expressly sold or transferred under a written agreement.

17.3. Customer-owned tooling and materials will be identified and handled separately from Shop-owned property.

17.4. The Shop is not required to retain Job-specific machine programs, temporary fixtures, prototypes, or production records indefinitely unless retention is expressly agreed.

17.5. Deliverable design files will be provided when expressly included in the agreed scope. Shop-specific machine programs, setup information, and proprietary process documentation are not automatically included.

17.6. Requests for additional tooling, programming, production files, or related deliverables that materially affect the approved scope or price require a change order.

18. CONFIDENTIALITY AND PROJECT DOCUMENTATION

18.1. Each party will exercise reasonable care to protect technical, commercial, and proprietary information identified as confidential or reasonably understood to be confidential from the circumstances.

18.2. Confidential information may be disclosed to employees, contractors, and service providers who reasonably require access to complete the Job and are subject to appropriate confidentiality obligations.

18.3. Confidentiality obligations do not apply to information that is publicly available without breach, independently developed, lawfully obtained from another source, or required to be disclosed by law.

18.4. The Shop may photograph or document work for quality assurance, production records, internal reference, and warranty administration.

18.5. The Shop will not publicly disclose confidential designs, proprietary project information, or Customer-identifying information for marketing purposes without consent.

18.6. Photographs of completed work may be used for promotional or portfolio purposes with Customer consent.

18.7. The Shop may retain quotations, approved change orders, drawings, specifications, invoices, communications, and other Job records for legitimate business, accounting, warranty, and legal purposes, subject to applicable law.

18.8. Non-disclosure agreements (NDAs) and non-compete agreements will not be signed to perform Customer work.

19. EVENTS BEYOND REASONABLE CONTROL

19.1. Neither party is responsible for delays or failures to perform caused by circumstances beyond its reasonable control, including natural disasters, fires, power outages, transportation disruptions, government restrictions, material or equipment shortages, labor disruptions, or comparable events.

19.2. The affected party shall notify the other party within a reasonable time and make reasonable efforts to mitigate the resulting disruption.

19.3. Payment obligations for goods delivered or services already performed remain enforceable to the extent permitted by law.

19.4. Material changes to scope, price, specifications, or schedule resulting from such circumstances shall be documented through a change order when the parties agree to continue under modified requirements.

19.5. If such circumstances substantially prevent completion for an extended period, either party may request termination or renegotiation, subject to a reasonable accounting of completed work, committed costs, and payments.

20. LIMITATION OF LIABILITY

20.1. Each party is responsible for losses resulting from its own breach of the Agreement, negligence, or other legally actionable conduct, subject to applicable law.

20.2. To the extent permitted by law, neither party shall be liable to the other for indirect, incidental, special, or consequential economic losses, including lost profits, lost business opportunities, or production downtime arising from the Job.

20.3. To the extent permitted by law, the Shop's aggregate contractual liability arising from the Job shall not exceed the amount paid or payable for that Job, including approved change orders.

20.4. The foregoing limitations do not apply to fraud, intentional misconduct, gross negligence where limitation is prohibited, personal injury or property damage claims that cannot lawfully be limited, or other liabilities that applicable law prohibits excluding or limiting.

20.5. Nothing in these Terms limits statutory consumer protections, mandatory warranties, or other rights and remedies that cannot lawfully be waived.

20.6. Any limitation or exclusive remedy applies only to the extent enforceable under applicable law and shall not operate where it fails its essential purpose.

21. DISPUTES AND RESOLUTION

21.1. The parties agree to make a good-faith effort to resolve disagreements through direct communication before pursuing formal legal proceedings.

21.2. A party asserting a dispute should provide written notice describing the issue and the requested resolution.

21.3. The parties will attempt to resolve the matter within 30 calendar days after notice unless circumstances reasonably require a different period.

21.4. The approved quotation, approved change orders, associated specifications, invoices, and documented communications may be considered in determining the agreed obligations.

21.5. Nothing in this section prevents either party from pursuing small claims proceedings, seeking emergency judicial relief, preserving legal rights, or exercising other remedies available under applicable law.

21.6. The Agreement is governed by the laws of the State of Washington (Lewis County), without regard to conflict-of-laws principles, except where mandatory law requires otherwise.

21.7. Any legal proceeding shall be brought before a court having proper jurisdiction and in a venue permitted by applicable law.

21.8. Each party shall bear its own legal expenses unless a statute, court order, or enforceable agreement provides otherwise.

22. COMMUNICATIONS AND NOTICES

22.1. Routine Job communications may occur by email, telephone, electronic messaging, or through the Shop's designated job management system.

22.2. Written approvals and notices, including quotation approvals and change orders, may be transmitted or accepted electronically unless a particular form of notice is required by law.

22.3. The Customer is responsible for maintaining accurate contact information and notifying the Shop of changes.

22.4. Notices concerning cancellation, disputes, completion, or other significant contractual matters should be sent through a documented communication method.

22.5. Electronic approvals and signatures may be treated as binding to the extent permitted by applicable law.

22.6. Electronic records maintained by the Shop may be used to document quotations, approvals, change orders, communications, and other transactions, subject to applicable evidentiary requirements.

23. GENERAL PROVISIONS

23.1. Entire Agreement. These Terms, the approved quotation, applicable specifications, and approved change orders constitute the complete Agreement between the parties regarding the Job.

23.2. Order of Precedence. If documents conflict, the most recently approved applicable change order controls first, followed by earlier approved change orders in reverse chronological order, the approved quotation and specifications, and then these Terms, unless the parties expressly agree otherwise. A change order supersedes earlier documents only to the extent of the changes it authorizes.

23.3. Amendments. Changes to the approved scope, specifications, price, schedule, or deliverables must be documented through an approved change order. Other material amendments require written agreement by both parties.

23.4. Severability. If any provision is found invalid or unenforceable, the remaining provisions will remain effective to the extent permitted by law.

23.5. No Waiver. Failure to enforce a provision on one occasion does not waive the right to enforce that provision later.

23.6. Assignment. Neither party may transfer its material obligations under the Agreement without the other party's written consent, except where permitted by law.

23.7. Independent Contractor. The Shop operates as an independent contractor and is not an employee, partner, or agent of the Customer.

23.8. Survival. Provisions concerning outstanding payments, intellectual property, confidentiality, liability, dispute resolution, and other obligations intended by their nature to survive shall remain effective after completion or termination.

23.9. Customer Purchase Orders. Additional or conflicting terms included in Customer purchase orders, acknowledgments, or other forms do not automatically modify the Agreement. Any modification requires express written acceptance by the Shop, subject to applicable law.

23.10. Mandatory Rights. Nothing in these Terms excludes or limits rights, warranties, obligations, or remedies that cannot lawfully be waived.

24. ACCEPTANCE

By approving the associated quotation or otherwise expressly accepting these Terms, the Customer acknowledges having had an opportunity to review the Agreement and authorizes the Shop to proceed in accordance with its provisions.

The Customer acknowledges that changes to the approved scope of work must be authorized through the change order process described in Section 3.

Acceptance may be documented through the Shop's quotation approval process, electronic signature, or another recorded written authorization.
"""


def seed() -> None:
    """Create a default template if there are none at all. Not legal advice; edit it."""
    from .models import AgreementTemplate, AgreementTemplateVersion

    if AgreementTemplate.all_objects.exists():
        return
    template = AgreementTemplate.objects.create(name="Standard terms", is_default=True)
    AgreementTemplateVersion.objects.create(template=template, version=1, body=DEFAULT_BODY)
