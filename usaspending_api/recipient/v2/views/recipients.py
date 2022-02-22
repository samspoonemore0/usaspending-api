import json
import logging
import uuid

from decimal import Decimal

from django.db.models import F
from django.conf import settings
from elasticsearch_dsl import A
from rest_framework.response import Response
from rest_framework.views import APIView

from usaspending_api.awards.v2.lookups.lookups import loan_type_mapping
from usaspending_api.broker.helpers.get_business_categories import get_business_categories
from usaspending_api.common.cache_decorator import cache_response
from usaspending_api.common.elasticsearch.search_wrappers import TransactionSearch
from usaspending_api.common.exceptions import InvalidParameterException
from usaspending_api.common.query_with_filters import QueryWithFilters
from usaspending_api.recipient.models import RecipientProfile, RecipientLookup, DUNS
from usaspending_api.recipient.v2.helpers import validate_year, reshape_filters, get_duns_business_types_mapping
from usaspending_api.recipient.v2.lookups import RECIPIENT_LEVELS, SPECIAL_CASES
from usaspending_api.references.models import RefCountryCode
from usaspending_api.search.models import TransactionSearch as TransactionSearchModel
from usaspending_api.search.v2.elasticsearch_helper import (
    get_scaled_sum_aggregations,
    get_number_of_unique_terms_for_transactions,
)

logger = logging.getLogger(__name__)


def validate_recipient_id(recipient_id):
    """Validate [duns+name]-[recipient_type] hash

    Args:
        recipient_id: str of the hash+duns to look up

    Returns:
        uuid of hash
        recipient level

    Raises:
        InvalidParameterException for invalid hashes
    """
    if "-" not in recipient_id:
        raise InvalidParameterException("ID ('{}') doesn't include Recipient-Level".format(hash))
    recipient_level = recipient_id[recipient_id.rfind("-") + 1 :]
    if recipient_level not in RECIPIENT_LEVELS:
        raise InvalidParameterException("Invalid Recipient-Level: '{}'".format(recipient_level))
    recipient_hash = recipient_id[: recipient_id.rfind("-")]
    try:
        uuid.UUID(recipient_hash)
    except ValueError:
        raise InvalidParameterException("Recipient Hash not valid UUID: '{}'.".format(recipient_hash))
    if not RecipientProfile.objects.filter(recipient_hash=recipient_hash, recipient_level=recipient_level).count():
        raise InvalidParameterException("Recipient ID not found: '{}'.".format(recipient_id))
    return recipient_hash, recipient_level


def extract_name_duns_from_hash(recipient_hash):
    """Extract the name and duns from the recipient hash

    Args:
        recipient_hash: uuid of the hash+duns to look up

    Returns:
        duns and name
    """
    name_duns_qs = (
        RecipientLookup.objects.filter(recipient_hash=recipient_hash).values("duns", "legal_business_name").first()
    )
    if not name_duns_qs:
        return None, None
    else:
        return name_duns_qs["duns"], name_duns_qs["legal_business_name"]


def extract_parents_from_hash(recipient_hash):
    """Extract the parent name and parent duns from the recipient hash

    Args:
        recipient_hash: uuid of the hash+duns to look up

    Returns:
        List of dictionaries (or empty)
            parent_id
            parent_duns
            parent_name
    """
    parents = []
    affiliations = (
        RecipientProfile.objects.filter(recipient_hash=recipient_hash, recipient_level="C")
        .values("recipient_affiliations")
        .first()
    )

    for duns in affiliations["recipient_affiliations"]:
        parent = RecipientLookup.objects.filter(duns=duns).values("recipient_hash", "legal_business_name").first()
        name, parent_id, uei = None, None, None

        if parent:
            name = parent["legal_business_name"]
            parent_id = "{}-P".format(parent["recipient_hash"])
            uei = RecipientProfile.objects.filter(recipient_hash=recipient_hash).values("uei").first()
            if uei is not None:
                uei = uei["uei"]

        parents.append({"parent_duns": duns, "parent_name": name, "parent_id": parent_id, "parent_uei": uei})
    return parents


def cleanup_location(location):
    """Various little fixes to cleanup the location object, given bad data from transactions

    Args:
        location: dictionary object representing the location

    Returns:
        dict of cleaned location info
    """
    # Older transactions mix country code and country name
    if location.get("country_code", None) == "UNITED STATES":
        location["country_code"] = "USA"
    # Country name generally isn't available with SAM data
    if location.get("country_code", None) and not location.get("country_name", None):
        country_name = RefCountryCode.objects.filter(country_code=location["country_code"]).values("country_name")
        location["country_name"] = country_name[0]["country_name"] if country_name else None
    # Older transactions have various formats for congressional code (13.0, 13, CA13)
    if location.get("congressional_code", None):
        congressional_code = location["congressional_code"]
        # remove post dot if that exists
        if "." in congressional_code:
            congressional_code = congressional_code[: congressional_code.rindex(".")]
        # [state abbr]-[congressional code]
        if len(congressional_code) == 4:
            congressional_code = congressional_code[2:]
        location["congressional_code"] = congressional_code
    return location


def extract_location(recipient_hash):
    """Extract the location data via the recipient hash

    Args:
        recipient_hash: uuid of the hash+duns to look up

    Returns:
        dict of location info
    """
    location = {
        "address_line1": None,
        "address_line2": None,
        "address_line3": None,
        "foreign_province": None,
        "city_name": None,
        "county_name": None,
        "state_code": None,
        "zip": None,
        "zip4": None,
        "foreign_postal_code": None,
        "country_name": None,
        "country_code": None,
        "congressional_code": None,
    }
    annotations = {
        "address_line1": F("address_line_1"),
        "address_line2": F("address_line_2"),
        "city_name": F("city"),
        "state_code": F("state"),
        "zip": F("zip5"),
        "congressional_code": F("congressional_district"),
    }
    values = [
        "address_line1",
        "address_line2",
        "city_name",
        "state_code",
        "zip",
        "zip4",
        "country_code",
        "congressional_code",
    ]
    found_location = (
        RecipientLookup.objects.filter(recipient_hash=recipient_hash).annotate(**annotations).values(*values).first()
    )
    if found_location:
        location.update(found_location)
        location = cleanup_location(location)
    return location


def extract_business_categories(recipient_name, recipient_duns, recipient_hash):
    """Extract the business categories via the recipient hash

    Args:
        recipient_name: name of the recipient
        recipient_duns: duns of the recipient
        recipient_hash: hash of name and duns

    Returns:
        list of business categories
    """
    business_categories = set()
    if recipient_name in SPECIAL_CASES:
        return list(business_categories)

    # Go through DUNS first
    d_business_cat = (
        DUNS.objects.filter(legal_business_name=recipient_name, awardee_or_recipient_uniqu=recipient_duns)
        .order_by("-update_date")
        .values("business_types_codes", "entity_structure")
        .first()
    )
    if d_business_cat:
        duns_types_mapping = get_duns_business_types_mapping()
        business_types_codes = d_business_cat["business_types_codes"]
        if d_business_cat["entity_structure"]:
            business_types_codes.append(d_business_cat["entity_structure"])
        business_types = {
            duns_types_mapping[type]: True
            for type in d_business_cat["business_types_codes"]
            if type in duns_types_mapping
        }
        business_categories |= set(get_business_categories(business_types, data_type="fpds"))

    # combine with latest transaction's business categories
    latest_transaction = (
        TransactionSearchModel.objects.filter(
            recipient_hash=recipient_hash, action_date__gte=settings.API_SEARCH_MIN_DATE
        )
        .order_by("-action_date", "-transaction_id")
        .values("business_categories")
        .first()
    )
    if latest_transaction and latest_transaction["business_categories"]:
        business_categories |= set(latest_transaction["business_categories"])

    return sorted(business_categories)


def obtain_recipient_totals(recipient_id, children=False, year="latest"):
    """Extract the total amount and transaction count for the recipient_hash given the time frame

    Args:
        recipient_id: string of hash(duns, name)-[recipient-level]
        children: whether or not to group by children
        year: the year the totals/counts are based on
    Returns:
        list of dictionaries representing hashes and their totals/counts
    """
    filters = reshape_filters(recipient_id=recipient_id, year=year)
    filter_query = QueryWithFilters.generate_transactions_elasticsearch_query(filters)

    search = TransactionSearch().filter(filter_query)

    if children:
        group_by_field = "recipient_agg_key"
    elif recipient_id[-2:] == "-P":
        group_by_field = "parent_recipient_hash"
    else:
        group_by_field = "recipient_hash"

    bucket_count = get_number_of_unique_terms_for_transactions(filter_query, f"{group_by_field}.hash")

    if bucket_count == 0:
        return []

    # Not setting the shard_size since the number of child recipients under a
    # parent recipient will not exceed 10k
    group_by_recipient = A("terms", field=group_by_field, size=bucket_count)

    sum_obligation = get_scaled_sum_aggregations("generated_pragmatic_obligation")["sum_field"]

    filter_loans = A("filter", terms={"type": list(loan_type_mapping.keys())})
    sum_face_value_loan = get_scaled_sum_aggregations("face_value_loan_guarantee")["sum_field"]

    search.aggs.bucket("group_by_recipient", group_by_recipient)
    search.aggs["group_by_recipient"].metric("sum_obligation", sum_obligation)
    search.aggs["group_by_recipient"].bucket("filter_loans", filter_loans)
    search.aggs["group_by_recipient"]["filter_loans"].metric("sum_face_value_loan", sum_face_value_loan)

    response = search.handle_execute()
    response_as_dict = response.aggs.to_dict()
    recipient_info_buckets = response_as_dict.get("group_by_recipient", {}).get("buckets", [])

    result_list = []

    for bucket in recipient_info_buckets:
        result = {}
        if children:
            recipient_info = json.loads(bucket.get("key"))
            hash_with_level = recipient_info.get("hash_with_level") or None
            uei = (
                RecipientProfile.objects.filter(recipient_hash=hash_with_level[:-2]).values("uei").first()
                if hash_with_level
                else None
            )
            if uei is not None:
                uei = uei["uei"]
            result = {
                "recipient_hash": hash_with_level[:-2] if hash_with_level else None,
                "recipient_unique_id": recipient_info.get("unique_id"),
                "uei": uei,
                "recipient_name": recipient_info.get("name"),
            }
        loan_info = bucket.get("filter_loans", {})
        result.update(
            {
                "total_obligation_amount": int(bucket.get("sum_obligation", {"value": 0})["value"]) / Decimal("100"),
                "total_obligation_count": bucket.get("doc_count", 0),
                "total_face_value_loan_amount": int(loan_info.get("sum_face_value_loan", {"value": 0})["value"])
                / Decimal("100"),
                "total_face_value_loan_count": loan_info.get("doc_count", 0),
            }
        )
        result_list.append(result)

    return result_list


class RecipientOverView(APIView):
    """
    This endpoint returns a high-level overview of a specific recipient, given its id.
    """

    endpoint_doc = "usaspending_api/api_contracts/contracts/v2/recipient/duns/recipient_id.md"

    @cache_response()
    def get(self, request, recipient_id):
        get_request = request.query_params
        year = validate_year(get_request.get("year", "latest"))
        recipient_hash, recipient_level = validate_recipient_id(recipient_id)
        recipient_duns, recipient_name = extract_name_duns_from_hash(recipient_hash)
        if not (recipient_name or recipient_duns):
            raise InvalidParameterException("Recipient Hash not found: '{}'.".format(recipient_hash))

        alternate_names = (
            RecipientLookup.objects.filter(recipient_hash=recipient_hash).values("alternate_names").first()
        )
        alternate_names = sorted(alternate_names.get("alternate_names", []))

        parents = []
        if recipient_level == "C":
            parents = extract_parents_from_hash(recipient_hash)
        elif recipient_level == "P":
            uei = RecipientProfile.objects.filter(recipient_hash=recipient_hash).values("uei").first()
            if uei is not None:
                uei = uei["uei"]
            parents = [
                {
                    "parent_id": recipient_id,
                    "parent_duns": recipient_duns,
                    "parent_uei": uei,
                    "parent_name": recipient_name,
                }
            ]

        location = extract_location(recipient_hash)
        business_types = extract_business_categories(recipient_name, recipient_duns, recipient_hash)
        results = obtain_recipient_totals(recipient_id, year=year)
        recipient_totals = results[0] if results else {}

        parent_id, parent_name, parent_duns, parent_uei = None, None, None, None
        if parents:
            parent_id = parents[0].get("parent_id")
            parent_name = parents[0].get("parent_name")
            parent_duns = parents[0].get("parent_duns")
            parent_uei = parents[0].get("parent_uei")

        uei = RecipientProfile.objects.filter(recipient_hash=recipient_hash).values("uei").first()
        if uei is not None:
            uei = uei["uei"]
        result = {
            "name": recipient_name,
            "alternate_names": alternate_names,
            "duns": recipient_duns,
            "uei": uei,
            "recipient_id": recipient_id,
            "recipient_level": recipient_level,
            "parent_id": parent_id,
            "parent_name": parent_name,
            "parent_duns": parent_duns,
            "parent_uei": parent_uei,
            "parents": parents,
            "business_types": business_types,
            "location": location,
            "total_transaction_amount": recipient_totals.get("total_obligation_amount", 0),
            "total_transactions": recipient_totals.get("total_obligation_count", 0),
            "total_face_value_loan_amount": recipient_totals.get("total_face_value_loan_amount", 0),
            "total_face_value_loan_transactions": recipient_totals.get("total_face_value_loan_count", 0),
        }
        return Response(result)


def extract_hash_name_from_duns(duns):
    """Extract the all the names and hashes associated with the DUNS provided

    Args:
        duns: duns to find the equivalent hash and name

    Returns:
        list of dictionaries containing hashes and names
    """
    qs_hash = RecipientLookup.objects.filter(duns=duns).values("recipient_hash", "legal_business_name").first()
    if not qs_hash:
        return None, None
    else:
        return qs_hash["recipient_hash"], qs_hash["legal_business_name"]


class ChildRecipients(APIView):
    """
    This endpoint returns a list of child recipients belonging to the given parent recipient DUNS.
    """

    endpoint_doc = "usaspending_api/api_contracts/contracts/v2/recipient/children/duns.md"

    @cache_response()
    def get(self, request, duns):
        get_request = request.query_params
        year = validate_year(get_request.get("year", "latest"))
        parent_hash, parent_name = extract_hash_name_from_duns(duns)
        if not parent_hash:
            raise InvalidParameterException("DUNS not found: '{}'.".format(duns))

        totals = obtain_recipient_totals("{}-P".format(parent_hash), children=True, year=year)

        # Get child info for each child DUNS
        results = []
        for total in totals:
            uei = RecipientProfile.objects.filter(recipient_hash=total["recipient_hash"]).values("uei").first()
            if uei is not None:
                uei = uei["uei"]
            results.append(
                {
                    "recipient_id": "{}-C".format(total["recipient_hash"]),
                    "name": total["recipient_name"],
                    "duns": total["recipient_unique_id"],
                    "uei": uei,
                    "amount": total["total_obligation_amount"],
                }
            )
        # Add children recipients without totals in this time period (if we already got all, ignore)
        if year != "all":
            # Get all possible child duns
            children_duns = RecipientProfile.objects.filter(recipient_hash=parent_hash, recipient_level="P").values(
                "recipient_affiliations"
            )
            if not children_duns:
                raise InvalidParameterException("DUNS is not listed as a parent: '{}'.".format(duns))
            children = children_duns[0]["recipient_affiliations"]

            # Gather their data points with Recipient Profile
            found_duns = [result["duns"] for result in results]
            missing_duns = [duns for duns in children if duns not in found_duns]
            missing_duns_qs = RecipientProfile.objects.filter(
                recipient_unique_id__in=missing_duns, recipient_level="C"
            ).values("recipient_hash", "recipient_name", "recipient_unique_id", "uei")
            for child_duns in list(missing_duns_qs):
                results.append(
                    {
                        "recipient_id": "{}-C".format(child_duns["recipient_hash"]),
                        "name": child_duns["recipient_name"],
                        "duns": child_duns["recipient_unique_id"],
                        "uei": child_duns["uei"],
                        "amount": 0,
                    }
                )

        # Add state/provinces to each result
        child_hashes = [result["recipient_id"][:-2] for result in results if result is not None]
        states_qs = RecipientLookup.objects.filter(recipient_hash__in=child_hashes).values("recipient_hash", "state")
        state_map = {str(state_result["recipient_hash"]): state_result["state"] for state_result in list(states_qs)}
        for result in results:
            recipient_hash = result["recipient_id"][:-2]
            if recipient_hash not in state_map:
                logger.warning("Recipient Hash not in state map: {}".format(recipient_hash))
            else:
                result["state_province"] = state_map[recipient_hash]

        return Response(results)
