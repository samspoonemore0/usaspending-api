from django.db.models import OuterRef, Q, Exists
from rest_framework.request import Request
from rest_framework.response import Response

from usaspending_api.accounts.models import FederalAccount
from usaspending_api.common.cache_decorator import cache_response
from usaspending_api.disaster.v2.views.disaster_base import DisasterBase
from usaspending_api.financial_activities.models import FinancialAccountsByProgramActivityObjectClass


class FederalAccountCountViewSet(DisasterBase):
    """
    Obtain the count of Federal Accounts related to supplied DEFC filter.
    """

    endpoint_doc = "usaspending_api/api_contracts/contracts/v2/disaster/federal_account/count.md"

    @cache_response()
    def post(self, request: Request) -> Response:
        filters = [
            Q(treasury_account__federal_account_id=OuterRef("pk")),
            Q(disaster_emergency_fund__code__in=self.def_codes),
            Q(submission__reporting_period_start__gte=self.reporting_period_min),
            Q(
                Q(
                    submission__quarter_format_flag=True,
                    submission__reporting_fiscal_year__lte=self.last_closed_quarterly_submission_dates.get(
                        "submission_fiscal_year"
                    ),
                    submission__reporting_fiscal_quarter__lte=self.last_closed_quarterly_submission_dates.get(
                        "submission_fiscal_quarter"
                    ),
                )
                | Q(
                    submission__quarter_format_flag=False,
                    submission__reporting_fiscal_year__lte=self.last_closed_monthly_submission_dates.get(
                        "submission_fiscal_year"
                    ),
                    submission__reporting_fiscal_period__lte=self.last_closed_monthly_submission_dates.get(
                        "submission_fiscal_month"
                    ),
                )
            ),
            Q(
                Q(obligations_incurred_by_program_object_class_cpe__gt=0)
                | Q(obligations_incurred_by_program_object_class_cpe__lt=0)
                | Q(gross_outlay_amount_by_program_object_class_cpe__gt=0)
                | Q(gross_outlay_amount_by_program_object_class_cpe__lt=0)
            ),
        ]
        count = (
            FederalAccount.objects.annotate(
                include=Exists(FinancialAccountsByProgramActivityObjectClass.objects.filter(*filters).values("pk"))
            )
            .filter(include=True)
            .values("pk")
            .count()
        )
        return Response({"count": count})