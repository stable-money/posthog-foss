from posthog.api.routing import RouterRegistry


def register_routes(routers: RouterRegistry) -> None:
    from products.experiments.backend.presentation.views import EnterpriseExperimentsViewSet

    routers.projects.register(r"experiments", EnterpriseExperimentsViewSet, "project_experiments", ["project_id"])

    # Holdouts and saved metrics are the only two viewsets that still live in `ee/`. Without
    # them the rest of the surface still works, so only they are conditional.
    try:
        from ee.clickhouse.views.experiment_holdouts import ExperimentHoldoutViewSet
        from ee.clickhouse.views.experiment_saved_metrics import ExperimentSavedMetricViewSet
    except ImportError:
        return

    routers.projects.register(
        r"experiment_holdouts", ExperimentHoldoutViewSet, "project_experiment_holdouts", ["project_id"]
    )
    routers.projects.register(
        r"experiment_saved_metrics", ExperimentSavedMetricViewSet, "project_experiment_saved_metrics", ["project_id"]
    )
