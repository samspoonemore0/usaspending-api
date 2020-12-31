from django.conf.urls import url
from usaspending_api.reporting.v2.views.agencies.agency_code.overview import AgencyOverview
from usaspending_api.reporting.v2.views.agencies.overview import AgenciesOverview
from usaspending_api.reporting.v2.views.differences import Differences
from usaspending_api.reporting.v2.views.submission_history import SubmissionHistory


urlpatterns = [
    url(r"^overview/$", AgenciesOverview.as_view()),
    url(r"^(?P<toptier_code>[0-9]{3,4})/differences/$", Differences.as_view()),
    url(r"^(?P<toptier_code>[0-9]{3,4})/overview/$", AgencyOverview.as_view()),
    url(
        r"^(?P<toptier_code>[0-9]{3,4})/(?P<fiscal_year>[0-9]{4})/(?P<fiscal_period>[0-9]{1,2})/submission_history/$",
        SubmissionHistory.as_view(),
    ),
]