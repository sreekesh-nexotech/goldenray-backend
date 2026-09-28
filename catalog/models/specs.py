"""The four spec tables (PLAN §2.2), one-to-one with ``catalog_component`` (CASCADE: a spec is part of its component),
and ``catalog_battery_family``.

Columns beyond the PLAN list hold every field of the legacy website models (``solar_panels``, ``solar_inverters``,
``batteries``), of the Flarize ``catalog.json`` items and of ``battery-master.json`` (mapping tables in
docs/decisions/catalog.md). Numeric widths follow the widest source (``kw``/``max_dc_input_kw`` keep watts exactly:
numeric(7,3)); unknown values stay NULL — nothing is invented.
"""

from django.db import models
from django.db.models import Q

from core.models import BaseModel

LIVE = Q(deleted_at__isnull=True)


def _one_to_one(related_name: str):
    # CASCADE: a spec row is a true child of its component.
    return models.OneToOneField("catalog.Component", on_delete=models.CASCADE, related_name=related_name)


def _pct(**kwargs):
    return models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True, **kwargs)


class PanelType(models.TextChoices):
    MONOCRYSTALLINE = "MONOCRYSTALLINE", "Monocrystalline"
    POLYCRYSTALLINE = "POLYCRYSTALLINE", "Polycrystalline"
    BIFACIAL = "BIFACIAL", "Bifacial"


class PanelTechnology(models.TextChoices):
    N_TYPE_TOPCON = "N_TYPE_TOPCON", "N-Type TOPCon"
    P_TYPE_PERC = "P_TYPE_PERC", "P-Type PERC"
    HJT = "HJT", "HJT"
    IBC = "IBC", "IBC"


class PanelSpec(BaseModel):
    component = _one_to_one("panel_spec")
    wattage_w = models.PositiveIntegerField()
    panel_type = models.CharField(max_length=24, choices=PanelType.choices, null=True, blank=True)
    technology = models.CharField(max_length=24, choices=PanelTechnology.choices, null=True, blank=True)
    cell_count = models.PositiveSmallIntegerField(null=True, blank=True)
    efficiency_pct = _pct()
    temperature_coefficient = models.DecimalField(max_digits=5, decimal_places=3, null=True, blank=True, help_text="Pmax %/°C.")
    temperature_coefficient_voc = models.DecimalField(max_digits=5, decimal_places=3, null=True, blank=True, help_text="Voc %/°C.")
    noct_c = models.SmallIntegerField(null=True, blank=True)
    real_output_at_60c_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    voc_v = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    vmp_v = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    isc_a = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    imp_a = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    max_system_voltage_v = models.PositiveIntegerField(null=True, blank=True)
    ip_rating = models.CharField(max_length=10, blank=True, default="")
    wind_load_pa = models.PositiveIntegerField(null=True, blank=True)
    snow_load_pa = models.PositiveIntegerField(null=True, blank=True)
    moisture_protection = models.CharField(max_length=50, blank=True, default="")
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    dimensions_mm = models.CharField(max_length=32, blank=True, default="")
    bifacial_gain_pct = models.SmallIntegerField(null=True, blank=True)
    first_year_drop_pct = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    annual_degradation_pct = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    output_at_year_25_pct = _pct()
    is_dcr = models.BooleanField(null=True, blank=True, help_text="Domestic Content Requirement (subsidy) module.")
    bis_certified = models.BooleanField(null=True, blank=True)
    bloomberg_tier1 = models.BooleanField(null=True, blank=True)
    pvel_top_performer = models.BooleanField(null=True, blank=True)
    independent_audit = models.BooleanField(null=True, blank=True)
    certifications = models.JSONField(default=list, blank=True)
    manufacturing_capacity = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        db_table = "catalog_panel_spec"
        constraints = [
            models.CheckConstraint(condition=Q(wattage_w__gt=0), name="catalog_panel_spec_wattage_positive"),
            models.CheckConstraint(condition=Q(panel_type__isnull=True) | Q(panel_type__in=PanelType.values), name="catalog_panel_spec_panel_type_valid"),
            models.CheckConstraint(condition=Q(technology__isnull=True) | Q(technology__in=PanelTechnology.values), name="catalog_panel_spec_technology_valid"),
            models.CheckConstraint(condition=Q(efficiency_pct__isnull=True) | (Q(efficiency_pct__gte=0) & Q(efficiency_pct__lte=100)), name="catalog_panel_spec_efficiency_pct"),
        ]


class InverterType(models.TextChoices):
    ONGRID = "ONGRID", "On-grid"
    HYBRID = "HYBRID", "Hybrid"


class InverterTopology(models.TextChoices):
    STRING = "STRING", "String"
    MICRO = "MICRO", "Microinverter"
    OPTIMIZED_STRING = "OPTIMIZED_STRING", "Optimised string"


class Phase(models.TextChoices):
    SINGLE = "1P", "Single phase"
    THREE = "3P", "Three phase"


class InverterSpec(BaseModel):
    component = _one_to_one("inverter_spec")
    kw = models.DecimalField(max_digits=7, decimal_places=3, help_text="Rated AC output in kW (watts kept exactly).")
    phase = models.CharField(max_length=8, choices=Phase.choices, null=True, blank=True)
    inverter_type = models.CharField(max_length=8, choices=InverterType.choices)
    topology = models.CharField(max_length=16, choices=InverterTopology.choices, null=True, blank=True)
    device_type = models.CharField(max_length=24, blank=True, default="", help_text="Flarize deviceType (per-panel allocation).")
    panels_per_device = models.PositiveSmallIntegerField(null=True, blank=True)
    mppt_count = models.PositiveSmallIntegerField(null=True, blank=True)
    mppt_voltage_min_v = models.PositiveIntegerField(null=True, blank=True)
    mppt_voltage_max_v = models.PositiveIntegerField(null=True, blank=True)
    start_voltage_v = models.PositiveIntegerField(null=True, blank=True)
    max_pv_voltage_v = models.PositiveIntegerField(null=True, blank=True)
    max_input_current_a = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    max_input_current_text = models.CharField(max_length=50, blank=True, default="", help_text="Display text, e.g. '16 A per MPPT'.")
    max_strings_per_mppt = models.PositiveSmallIntegerField(null=True, blank=True)
    max_isc_a = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    max_dc_input_kw = models.DecimalField(max_digits=7, decimal_places=3, null=True, blank=True)
    battery_voltage_v = models.PositiveIntegerField(null=True, blank=True)
    max_charge_current_a = models.PositiveIntegerField(null=True, blank=True)
    efficiency_pct = _pct(help_text="Maximum efficiency.")
    european_efficiency_pct = _pct()
    mppt_efficiency_pct = _pct()
    dc_oversizing_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    ac_overloading_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    ip_rating = models.CharField(max_length=10, blank=True, default="")
    display = models.CharField(max_length=100, blank=True, default="")
    suitable_system_size = models.CharField(max_length=100, blank=True, default="")
    pid_protection = models.BooleanField(null=True, blank=True)
    iv_curve_scanning = models.CharField(max_length=100, blank=True, default="")
    corrosion_protection = models.CharField(max_length=100, blank=True, default="")
    operating_temperature = models.CharField(max_length=50, blank=True, default="")
    cooling = models.CharField(max_length=100, blank=True, default="")
    noise_level = models.CharField(max_length=50, blank=True, default="")
    dc_surge_protection = models.CharField(max_length=50, blank=True, default="")
    ac_surge_protection = models.CharField(max_length=50, blank=True, default="")
    arc_fault_detection = models.CharField(max_length=50, blank=True, default="")
    grid_protection = models.BooleanField(null=True, blank=True)
    monitoring_app = models.CharField(max_length=100, blank=True, default="")
    real_time_monitoring = models.BooleanField(null=True, blank=True)
    remote_diagnostics = models.CharField(max_length=100, blank=True, default="")
    firmware_updates = models.CharField(max_length=100, blank=True, default="")
    connectivity = models.CharField(max_length=100, blank=True, default="")
    certifications = models.JSONField(default=list, blank=True)
    brand_trust = models.CharField(max_length=50, blank=True, default="")
    year_founded = models.PositiveSmallIntegerField(null=True, blank=True)
    countries_served = models.CharField(max_length=50, blank=True, default="")
    global_installations = models.CharField(max_length=50, blank=True, default="")
    communication = models.JSONField(default=list, blank=True, help_text="Supported protocols/interfaces.")
    compatible_battery_families = models.JSONField(default=list, blank=True, help_text="catalog_battery_family slugs (battery-compatibility engine).")
    system_controller_required = models.BooleanField(null=True, blank=True, help_text="PBC-M-003; NULL = not recorded.")

    class Meta:
        db_table = "catalog_inverter_spec"
        constraints = [
            models.CheckConstraint(condition=Q(kw__gt=0), name="catalog_inverter_spec_kw_positive"),
            models.CheckConstraint(condition=Q(phase__isnull=True) | Q(phase__in=Phase.values), name="catalog_inverter_spec_phase_valid"),
            models.CheckConstraint(condition=Q(inverter_type__in=InverterType.values), name="catalog_inverter_spec_type_valid"),
            models.CheckConstraint(condition=Q(topology__isnull=True) | Q(topology__in=InverterTopology.values), name="catalog_inverter_spec_topology_valid"),
        ]


class VoltageClass(models.TextChoices):
    V48 = "48V", "48 V (low voltage)"
    HV = "HV", "High voltage"


class BatteryFamily(BaseModel):
    """``catalog_battery_family``: the compatibility unit the battery engine checks inverters against."""

    slug = models.SlugField(max_length=64)
    name = models.CharField(max_length=120)
    voltage_class = models.CharField(max_length=8, choices=VoltageClass.choices)
    notes = models.TextField(blank=True, default="")

    class Meta:
        db_table = "catalog_battery_family"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["slug"], condition=LIVE, name="catalog_battery_family_slug_live_uniq"),
            models.CheckConstraint(condition=Q(voltage_class__in=VoltageClass.values), name="catalog_battery_family_voltage_class_valid"),
        ]

    def __str__(self) -> str:
        return self.name


class Chemistry(models.TextChoices):
    LFP = "LFP", "Lithium iron phosphate"
    LEAD_ACID = "LEAD_ACID", "Lead acid"
    NMC = "NMC", "Lithium NMC"


class EngineeringStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PENDING_ENGINEERING_APPROVAL = "PENDING_ENGINEERING_APPROVAL", "Pending engineering approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    WITHDRAWN = "WITHDRAWN", "Withdrawn"


class ProcurementStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    INACTIVE = "INACTIVE", "Inactive"
    DISCONTINUED = "DISCONTINUED", "Discontinued"
    PENDING_SOURCING = "PENDING_SOURCING", "Pending sourcing"


def _amps():
    return models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)


def _volts():
    return models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)


class BatterySpec(BaseModel):
    """Battery technical data plus the Flarize battery-master engineering overlay (34 fields)."""

    component = _one_to_one("battery_spec")
    # PROTECT: a family referenced by a battery cannot disappear. Nullable: battery-master.json names no families.
    family = models.ForeignKey(BatteryFamily, null=True, blank=True, on_delete=models.PROTECT, related_name="batteries")
    chemistry = models.CharField(max_length=16, choices=Chemistry.choices, null=True, blank=True)
    battery_type = models.CharField(max_length=32, blank=True, default="")
    nominal_voltage_v = _volts()
    min_voltage_v = _volts()
    max_voltage_v = _volts()
    capacity_kwh = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    usable_kwh = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    backup_hours = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True, help_text="Website calculator backup time (legacy batteries.backup_hour).")
    max_c_rate = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    cycle_life = models.PositiveIntegerField(null=True, blank=True)
    dod_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    stackable = models.BooleanField(null=True, blank=True)
    max_units_in_series = models.PositiveSmallIntegerField(null=True, blank=True)
    max_units_in_parallel = models.PositiveSmallIntegerField(null=True, blank=True)
    bms_included = models.BooleanField(null=True, blank=True)
    continuous_charge_current_a = _amps()
    continuous_discharge_current_a = _amps()
    maximum_charge_current_a = _amps()
    maximum_discharge_current_a = _amps()
    peak_current_a = _amps()
    integrated_protection = models.BooleanField(null=True, blank=True)
    protection_type = models.CharField(max_length=32, blank=True, default="")
    protection_rating = models.CharField(max_length=12, blank=True, default="")
    external_protection_required = models.BooleanField(null=True, blank=True)
    communication_protocol = models.CharField(max_length=64, blank=True, default="")
    communication_required = models.BooleanField(null=True, blank=True)
    # NULL = not recorded (INDETERMINATE for the compatibility checks); [] = recorded as none.
    compatible_inverters = models.JSONField(null=True, blank=True, help_text="Component SKUs; null = unknown.")
    compatible_system_types = models.JSONField(null=True, blank=True)
    compatible_phases = models.JSONField(null=True, blank=True)
    architecture = models.CharField(max_length=24, blank=True, default="")
    engineering_status = models.CharField(max_length=32, choices=EngineeringStatus.choices, default=EngineeringStatus.PENDING_ENGINEERING_APPROVAL)
    procurement_status = models.CharField(max_length=20, choices=ProcurementStatus.choices, default=ProcurementStatus.ACTIVE)
    engineering_notes = models.TextField(blank=True, default="")
    open_items = models.JSONField(default=list, blank=True)
    status_history = models.JSONField(default=list, blank=True)
    supplier = models.CharField(max_length=120, blank=True, default="")
    supplier_reference = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        db_table = "catalog_battery_spec"
        constraints = [
            models.CheckConstraint(condition=Q(chemistry__isnull=True) | Q(chemistry__in=Chemistry.values), name="catalog_battery_spec_chemistry_valid"),
            models.CheckConstraint(condition=Q(engineering_status__in=EngineeringStatus.values), name="catalog_battery_spec_engineering_status_valid"),
            models.CheckConstraint(condition=Q(procurement_status__in=ProcurementStatus.values), name="catalog_battery_spec_procurement_status_valid"),
            models.CheckConstraint(condition=Q(dod_pct__isnull=True) | Q(dod_pct__lte=100), name="catalog_battery_spec_dod_pct"),
        ]


class StructureType(models.TextChoices):
    FLAT_ROOF = "FLAT_ROOF", "Flat roof"
    ELEVATED = "ELEVATED", "Elevated"
    SHEET_ROOF = "SHEET_ROOF", "Sheet roof"
    GROUND = "GROUND", "Ground mount"


class StructureMaterial(models.TextChoices):
    GP = "GP", "Galvanised plain (GP)"
    GI = "GI", "Galvanised iron (GI)"
    AL = "AL", "Aluminium"


class StructureSpec(BaseModel):
    component = _one_to_one("structure_spec")
    structure_type = models.CharField(max_length=16, choices=StructureType.choices, null=True, blank=True)
    material = models.CharField(max_length=8, choices=StructureMaterial.choices, null=True, blank=True)
    tube_size = models.CharField(max_length=12, blank=True, default="")
    weight_kg_per_m = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    structure_role = models.CharField(max_length=24, blank=True, default="", help_text="Flarize structureRole (e.g. PRIMARY_TUBE).")
    specification = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "catalog_structure_spec"
        constraints = [
            models.CheckConstraint(condition=Q(structure_type__isnull=True) | Q(structure_type__in=StructureType.values), name="catalog_structure_spec_type_valid"),
            models.CheckConstraint(condition=Q(material__isnull=True) | Q(material__in=StructureMaterial.values), name="catalog_structure_spec_material_valid"),
        ]
