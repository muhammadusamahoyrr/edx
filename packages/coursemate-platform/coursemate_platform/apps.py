"""Open edX plugin registration — design §3.4 rule 4.

`PluginSignals.CONFIG` is keyed by project type, each with its own RELATIVE_PATH.
That is not incidental: it is exactly the mechanism that makes the cms/lms
receiver split correct rather than stylistic.

An earlier draft of the design placed every receiver in the CMS, which would have
left enrollment changes unobserved entirely — the metadata cache invalidates on
enrollment (§6.4) and §10.7 scopes personal data on unenrollment, and both need an
LMS-side receiver. Two RELATIVE_PATHs, two processes, one bug avoided.
"""

from django.apps import AppConfig
from edx_django_utils.plugins.constants import (
    PluginSettings,
    PluginSignals,
    PluginURLs,
)
from openedx.core.djangoapps.plugins.constants import ProjectType, SettingsType


class CourseMateConfig(AppConfig):
    name = "coursemate_platform"
    verbose_name = "CourseMate"

    plugin_app = {
        PluginURLs.CONFIG: {
            ProjectType.CMS: {
                PluginURLs.NAMESPACE: "coursemate",
                PluginURLs.REGEX: r"^coursemate/",
                PluginURLs.RELATIVE_PATH: "urls",
            },
        },
        PluginSettings.CONFIG: {
            ProjectType.LMS: {
                SettingsType.COMMON: {PluginSettings.RELATIVE_PATH: "settings.common"},
                SettingsType.PRODUCTION: {
                    PluginSettings.RELATIVE_PATH: "settings.production"
                },
            },
            ProjectType.CMS: {
                SettingsType.COMMON: {PluginSettings.RELATIVE_PATH: "settings.common"},
                SettingsType.PRODUCTION: {
                    PluginSettings.RELATIVE_PATH: "settings.production"
                },
            },
        },
        PluginSignals.CONFIG: {
            # --- Studio: content_authoring events -----------------------------
            # Every receiver here does ONE thing: validate and enqueue. Running
            # extract/chunk/embed inline would hang the instructor's Publish
            # button on third-party network I/O (§5.2).
            ProjectType.CMS: {
                PluginSignals.RELATIVE_PATH: "events.cms_receivers",
                PluginSignals.RECEIVERS: [
                    {
                        PluginSignals.RECEIVER_FUNC_NAME: "on_xblock_published",
                        PluginSignals.SIGNAL_PATH:
                            "openedx_events.content_authoring.signals.XBLOCK_PUBLISHED",
                        PluginSignals.DISPATCH_UID: "coursemate_xblock_published",
                    },
                    {
                        PluginSignals.RECEIVER_FUNC_NAME: "on_xblock_deleted",
                        PluginSignals.SIGNAL_PATH:
                            "openedx_events.content_authoring.signals.XBLOCK_DELETED",
                        PluginSignals.DISPATCH_UID: "coursemate_xblock_deleted",
                    },
                    {
                        PluginSignals.RECEIVER_FUNC_NAME: "on_xblock_duplicated",
                        PluginSignals.SIGNAL_PATH:
                            "openedx_events.content_authoring.signals.XBLOCK_DUPLICATED",
                        PluginSignals.DISPATCH_UID: "coursemate_xblock_duplicated",
                    },
                    # NOT subscribed, deliberately: XBLOCK_CREATED and
                    # XBLOCK_UPDATED. They fire on DRAFT edits. The platform's own
                    # search app does subscribe to them, because Studio search
                    # indexes drafts — we are published-only (Principle 3), so we
                    # diverge on purpose. See design §5.4.
                ],
            },
            # --- LMS: learning events -----------------------------------------
            ProjectType.LMS: {
                PluginSignals.RELATIVE_PATH: "events.lms_receivers",
                PluginSignals.RECEIVERS: [
                    {
                        PluginSignals.RECEIVER_FUNC_NAME: "on_unenrollment",
                        PluginSignals.SIGNAL_PATH:
                            "openedx_events.learning.signals.COURSE_UNENROLLMENT_COMPLETED",
                        PluginSignals.DISPATCH_UID: "coursemate_unenrollment",
                    },
                ],
            },
        },
    }
