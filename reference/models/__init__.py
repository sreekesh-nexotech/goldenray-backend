"""Reference data: pincodes (+ post offices), KSEB tariff slabs, device types, wattages, room sizes, EVs, appliances."""

from reference.models.appliance import Appliance
from reference.models.lists import DeviceType, EvCar, EvScooter, RoomSize, Wattage
from reference.models.pincode import Pincode, PincodeOffice
from reference.models.tariff import KsebTariff

__all__ = ["Appliance", "DeviceType", "EvCar", "EvScooter", "KsebTariff", "Pincode", "PincodeOffice", "RoomSize", "Wattage"]
