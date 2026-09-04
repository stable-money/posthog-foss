from posthog.api.routing import RouterRegistry


def register_routes(routers: RouterRegistry) -> None:
    """No routes.

    This product only exposed the Max tool registry, which is part of the enterprise code
    this build does not contain.
    """
    return
