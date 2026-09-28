"""Leads models: website enquiries (+ notes, events), OTP requests, affiliate applications, warranty requests and
customer installations."""

from leads.models.choices import KeralaDistrict
from leads.models.forms import AffiliateApplication, IssueType, Profession, WarrantyRequest
from leads.models.installation import CustomerInstallation
from leads.models.lead import Lead, LeadEvent, LeadNote
from leads.models.otp import OtpRequest

__all__ = ["AffiliateApplication", "CustomerInstallation", "IssueType", "KeralaDistrict", "Lead", "LeadEvent", "LeadNote", "OtpRequest", "Profession", "WarrantyRequest"]
