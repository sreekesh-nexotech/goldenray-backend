"""Careers models: departments, job positions (+ SEO), job applications with notes and an append-only timeline."""

from careers.models.application import GENERAL_APPLICATION, JobApplication, JobApplicationEvent, JobApplicationNote
from careers.models.department import Department
from careers.models.position import JobPosition

__all__ = ["GENERAL_APPLICATION", "Department", "JobApplication", "JobApplicationEvent", "JobApplicationNote", "JobPosition"]
