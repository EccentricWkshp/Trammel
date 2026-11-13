from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Router, Schema, Status

from trammel.api.types import choices
from trammel.core import access, audit
from trammel.core.services import save_audited

from .models import Machine, Material, Vendor

router = Router(tags=["machines and materials"])


class MachineOut(ModelSchema):
    class Meta:
        model = Machine
        fields = [
            "id",
            "name",
            "kind",
            "group",
            "runs_unattended",
            "hourly_rate",
            "is_active",
            "notes",
        ]


class MachineIn(Schema):
    name: str
    kind: choices(Machine.Kind) = Machine.Kind.OTHER
    #: Machines in the same group can stand in for each other on the schedule.
    group: str = ""
    #: Once started, runs on its own past closing (a 3D printer overnight).
    runs_unattended: bool = False
    hourly_rate: Decimal = Decimal("0")
    is_active: bool = True
    notes: str = ""


class VendorOut(ModelSchema):
    class Meta:
        model = Vendor
        fields = [
            "id",
            "name",
            "contact_name",
            "email",
            "phone",
            "website",
            "account_number",
            "payment_terms",
            "lead_time_days",
            "address",
            "is_active",
            "notes",
        ]


class VendorIn(Schema):
    name: str
    contact_name: str = ""
    email: str = ""
    phone: str = ""
    website: str = ""
    account_number: str = ""
    payment_terms: str = ""
    lead_time_days: int | None = None
    address: str = ""
    is_active: bool = True
    notes: str = ""


class MaterialOut(ModelSchema):
    #: Null without permission to see costs (core.see_costs).
    unit_cost: Decimal | None = None

    class Meta:
        model = Material
        fields = [
            "id",
            "name",
            "category",
            "sku",
            "vendor",
            "url",
            "unit",
            "markup",
            "sheet_width",
            "sheet_length",
            "sheet_dimension_unit",
            "thickness",
            "is_active",
            "notes",
        ]

    @staticmethod
    def resolve_unit_cost(obj, context):
        return obj.unit_cost if access.sees_costs(context) else None


class MaterialIn(Schema):
    name: str
    category: Material.Category = Material.Category.OTHER
    sku: str = ""
    vendor_id: UUID | None = None
    url: str = ""
    unit: str = "each"
    unit_cost: Decimal = Decimal("0")
    markup: Decimal | None = None
    sheet_width: Decimal | None = None
    sheet_length: Decimal | None = None
    sheet_dimension_unit: str = ""
    thickness: str = ""
    is_active: bool = True
    notes: str = ""


#: One permission per kind covers adding, editing, archiving and deleting it.
PERMS = {
    Machine: "shop.manage_machines",
    Material: "shop.manage_materials",
    Vendor: "shop.manage_vendors",
}


def _upsert(request, model, pk, payload):
    if pk is None:
        access.require(request.actor, PERMS[model])
        return save_audited(model(**payload.dict()))
    obj = access.get_or_404(request.actor, PERMS[model], model.objects.all(), pk=pk)
    before = audit.snapshot(obj)
    for field, value in payload.dict().items():
        setattr(obj, field, value)
    return save_audited(obj, before=before)


@router.get("/machines", response=list[MachineOut])
def list_machines(request):
    return access.scoped(request.actor, "shop.view", Machine.objects.all())


@router.post("/machines", response={201: MachineOut})
def create_machine(request, payload: MachineIn):
    return Status(201, _upsert(request, Machine, None, payload))


@router.put("/machines/{machine_id}", response=MachineOut)
def update_machine(request, machine_id: UUID, payload: MachineIn):
    return _upsert(request, Machine, machine_id, payload)


@router.get("/vendors", response=list[VendorOut])
def list_vendors(request):
    return access.scoped(request.actor, "shop.view", Vendor.objects.all())


@router.post("/vendors", response={201: VendorOut})
def create_vendor(request, payload: VendorIn):
    return Status(201, _upsert(request, Vendor, None, payload))


@router.put("/vendors/{vendor_id}", response=VendorOut)
def update_vendor(request, vendor_id: UUID, payload: VendorIn):
    return _upsert(request, Vendor, vendor_id, payload)


@router.get("/materials", response=list[MaterialOut])
def list_materials(request):
    return access.scoped(request.actor, "shop.view", Material.objects.all())


@router.post("/materials", response={201: MaterialOut})
def create_material(request, payload: MaterialIn):
    return Status(201, _upsert(request, Material, None, payload))


@router.put("/materials/{material_id}", response=MaterialOut)
def update_material(request, material_id: UUID, payload: MaterialIn):
    return _upsert(request, Material, material_id, payload)
