from django.apps import AppConfig


class ProjectsConfig(AppConfig):
    name = "projects"
    label = "projects"
    verbose_name = "Projects"

    def ready(self):
        from customers.services import merge
        from projects.models import Project
        from projects.services import customer_timeline  # noqa: F401 - registers the customer timeline provider

        # A customer merge re-points projects to the surviving customer; a customer with live projects cannot be deleted.
        merge.register_dependant(Project, "customer", blocks_delete=True)
