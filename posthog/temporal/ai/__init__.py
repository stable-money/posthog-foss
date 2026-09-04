from posthog.temporal.ai.anomaly_investigation import AnomalyInvestigationWorkflow, investigate_anomaly_activity
from posthog.temporal.ai.slack_app import SLACK_APP_ACTIVITIES
from posthog.temporal.ai.slack_app.posthog_code_slack_mention import PostHogCodeSlackMentionWorkflow
from posthog.temporal.ai.slack_app.posthog_code_slack_mention_command import PostHogCodeSlackMentionCommandWorkflow
from posthog.temporal.ai.slack_app.posthog_slack_inbox_onboarding import PostHogSlackInboxOnboardingWorkflow
from posthog.temporal.ai.slack_app.slack_app_fork import SlackAppForkThreadWorkflow
from posthog.temporal.ai.slack_app.slack_app_mention import SlackAppMentionWorkflow

# PostHog Desktop Slack workflows live on TASKS_TASK_QUEUE alongside ProcessTaskWorkflow,
# the worker they hand off to once a repo is picked. The subset is kept exported so
# start_temporal_worker can register it on that queue without pulling in unrelated AI
# workflows.
POSTHOG_CODE_SLACK_WORKFLOWS = [
    PostHogCodeSlackMentionWorkflow,
    SlackAppMentionWorkflow,
    PostHogCodeSlackMentionCommandWorkflow,
    SlackAppForkThreadWorkflow,
    PostHogSlackInboxOnboardingWorkflow,
]

POSTHOG_CODE_SLACK_ACTIVITIES = [*SLACK_APP_ACTIVITIES]

AI_WORKFLOWS = [
    AnomalyInvestigationWorkflow,
]

AI_ACTIVITIES = [
    investigate_anomaly_activity,
]

__all__ = [
]
