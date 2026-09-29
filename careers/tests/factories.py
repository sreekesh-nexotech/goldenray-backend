import factory
from django.utils import timezone

from careers.models import Department, JobApplication, JobApplicationNote, JobPosition


class DepartmentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Department

    name = factory.Sequence(lambda n: f"Department {n}")
    slug = factory.Sequence(lambda n: f"department-{n}")


class JobPositionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = JobPosition

    title = factory.Sequence(lambda n: f"Solar Engineer {n}")
    slug = factory.Sequence(lambda n: f"solar-engineer-{n}")
    department = factory.SubFactory(DepartmentFactory)
    location = "Kochi"
    description = "Design rooftop systems."
    experience_required = "1-3 years"
    responsibilities = "Plan work\nExecute safely"
    requirements = "Diploma"
    benefits = "Health cover"
    status = JobPosition.Status.DRAFT

    class Params:
        published = factory.Trait(status=JobPosition.Status.PUBLISHED, published_at=factory.LazyFunction(timezone.now))
        closed = factory.Trait(status=JobPosition.Status.CLOSED, published_at=factory.LazyFunction(timezone.now), closed_at=factory.LazyFunction(timezone.now))


class JobApplicationFactory(factory.django.DjangoModelFactory):
    """A row only (no stored files); the API tests upload real files through the public endpoint."""

    class Meta:
        model = JobApplication

    position = factory.SubFactory(JobPositionFactory, published=True)
    position_label = factory.LazyAttribute(lambda application: application.position.title if application.position else "General application")
    position_title = factory.LazyAttribute(lambda application: application.position.title if application.position else "")
    department_name = factory.LazyAttribute(lambda application: application.position.department.name if application.position else "")
    name = factory.Faker("name")
    email = factory.Sequence(lambda n: f"candidate{n}@example.com")
    phone_e164 = factory.Sequence(lambda n: f"+9198{n:08d}")
    location = "Alappuzha"
    linkedin = factory.Sequence(lambda n: f"https://linkedin.com/in/candidate-{n}")
    declaration_accepted = True


class JobApplicationNoteFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = JobApplicationNote

    application = factory.SubFactory(JobApplicationFactory)
    body = "Strong on-site experience."
    author_name = "HR"
