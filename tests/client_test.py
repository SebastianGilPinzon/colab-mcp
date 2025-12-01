import json
import unittest
from unittest.mock import MagicMock, patch
import uuid


from colab_mcp.client import (
    Accelerator,
    CcuInfo,
    ColabClient,
    ListedAssignments,
    Outcome,
    PostAssignmentResponse,
    Shape,
    SubscriptionState,
    SubscriptionTier,
    Variant,
)

COLAB_HOST = "https://colab.example.com"
GOOGLE_APIS_HOST = "https://colab.example.googleapis.com"
BEARER_TOKEN = "access-token"
NOTEBOOK_HASH = uuid.uuid4()

DEFAULT_ASSIGNMENT_RESPONSE = {
    "accelerator": Accelerator.A100,
    "endpoint": "mock-server",
    "fit": 30,
    "sub": SubscriptionState.UNSUBSCRIBED,
    "subTier": SubscriptionTier.NONE,
    "variant": Variant.GPU,
    "machineShape": Shape.STANDARD,
    "runtimeProxyInfo": {
        "token": "mock-token",
        "tokenExpiresInSeconds": 42,
        "url": "https://mock-url.com",
    },
}


DEFAULT_LIST_ASSIGNMENTS_RESPONSE = ListedAssignments(
    assignments=[
        {
            "accelerator": DEFAULT_ASSIGNMENT_RESPONSE["accelerator"],
            "endpoint": DEFAULT_ASSIGNMENT_RESPONSE["endpoint"],
            "variant": DEFAULT_ASSIGNMENT_RESPONSE["variant"],
            "machineShape": DEFAULT_ASSIGNMENT_RESPONSE["machineShape"],
        }
    ]
)


DEFAULT_ASSIGNMENT = PostAssignmentResponse(
    accelerator=DEFAULT_ASSIGNMENT_RESPONSE["accelerator"],
    endpoint=DEFAULT_ASSIGNMENT_RESPONSE["endpoint"],
    idleTimeoutSec=DEFAULT_ASSIGNMENT_RESPONSE["fit"],
    subscriptionState=DEFAULT_ASSIGNMENT_RESPONSE["sub"],
    subscriptionTier=DEFAULT_ASSIGNMENT_RESPONSE["subTier"],
    variant=DEFAULT_ASSIGNMENT_RESPONSE["variant"],
    machineShape=DEFAULT_ASSIGNMENT_RESPONSE["machineShape"],
    runtimeProxyInfo=DEFAULT_ASSIGNMENT_RESPONSE["runtimeProxyInfo"],
    outcome=Outcome.SUCCESS,
)


def with_xssi(response):
    return f")]}}'\n{response}"


class TestColabClient(unittest.TestCase):
    def setUp(self):
        self.session_mock = MagicMock()
        self.session_mock.get.return_value = BEARER_TOKEN
        self.client = ColabClient(
            colab_domain=COLAB_HOST,
            colab_api_domain=GOOGLE_APIS_HOST,
            get_access_token=self.session_mock.get,
        )

    @patch("requests.Session.request")
    def test_get_subscription_tier(self, mock_request):
        mock_response = {
            "subscriptionTier": "SUBSCRIPTION_TIER_NONE",
            "paidComputeUnitsBalance": 0,
        }
        mock_request.return_value.ok = True
        mock_request.return_value.text = with_xssi(json.dumps(mock_response))

        tier = self.client.get_subscription_tier()
        self.assertEqual(tier, SubscriptionTier.NONE)
        mock_request.assert_called_once()

    @patch("requests.Session.request")
    def test_get_ccu_info(self, mock_request):
        mock_response = {
            "currentBalance": 1,
            "consumptionRateHourly": 2,
            "assignmentsCount": 3,
        }
        mock_request.return_value.ok = True
        mock_request.return_value.text = with_xssi(json.dumps(mock_response))

        ccu_info = self.client.get_ccu_info()
        self.assertEqual(ccu_info, CcuInfo(**mock_response))
        mock_request.assert_called_once()

    @patch("requests.Session.request")
    def test_list_assignments(self, mock_request):
        mock_request.return_value.ok = True
        mock_request.return_value.text = with_xssi(
            DEFAULT_LIST_ASSIGNMENTS_RESPONSE.model_dump_json(by_alias=True)
        )

        assignments = self.client.list_assignments()
        self.assertEqual(assignments, DEFAULT_LIST_ASSIGNMENTS_RESPONSE.assignments)
        mock_request.assert_called_once()

    @patch("colab_mcp.client.ColabClient._post_assignment")
    @patch("colab_mcp.client.ColabClient._get_assignment")
    def test_assign_creates_new(self, mock_get_assignment, mock_post_assignment):
        mock_get_assignment.return_value = MagicMock(xsrf_token="mock-xsrf-token")
        mock_post_assignment.return_value = DEFAULT_ASSIGNMENT

        result = self.client.assign(NOTEBOOK_HASH, Variant.DEFAULT)
        self.assertEqual(result["assignment"], DEFAULT_ASSIGNMENT)
        self.assertTrue(result["is_new"])


if __name__ == "__main__":
    unittest.main()
