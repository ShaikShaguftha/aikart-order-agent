import os
import sys
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

from integrations.providers.shopify import ShopifyConnector


def create_pending_payment_order():
    connector = ShopifyConnector()
    print(f"Connecting to shop: {connector.shop_domain}")

    draft_mutation = """
    mutation draftOrderCreate($input: DraftOrderInput!) {
      draftOrderCreate(input: $input) {
        draftOrder {
          id
          name
        }
        userErrors {
          field
          message
        }
      }
    }
    """

    draft_input = {
        "lineItems": [
            {
                "title": "Test Pending Item (Unpaid)",
                "originalUnitPrice": "15.00",
                "quantity": 1,
            }
        ],
        "note": "Test order created for safe action cancellation testing (Unpaid)",
    }

    res = connector._execute_graphql(draft_mutation, {"input": draft_input})
    print(f"Raw GraphQL response: {res}")
    if "error" in res:
        print(f"Failed to create draft order: {res['error']}")
        return None

    draft_data = res.get("draftOrderCreate")
    if not draft_data:
        print("No draftOrderCreate in response.")
        return None

    user_errors = draft_data.get("userErrors", [])
    if user_errors:
        print(f"Draft Order User Errors: {user_errors}")
        return None

    draft_order = draft_data.get("draftOrder")
    if not draft_order:
        print("No draft order returned.")
        return None

    draft_gid = draft_order["id"]
    print(f"Created Draft Order: {draft_order['name']} ({draft_gid})")

    complete_mutation = """
    mutation draftOrderComplete($id: ID!, $paymentPending: Boolean) {
      draftOrderComplete(id: $id, paymentPending: $paymentPending) {
        draftOrder {
          id
          order {
            id
            name
            displayFinancialStatus
            displayFulfillmentStatus
            fullyPaid
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """

    res_comp = connector._execute_graphql(
        complete_mutation, {"id": draft_gid, "paymentPending": True}
    )
    print(f"Complete Raw Response: {res_comp}")
    if "error" in res_comp:
        print(f"Failed to complete draft order: {res_comp['error']}")
        return None

    comp_data = res_comp.get("draftOrderComplete") or {}
    comp_errors = comp_data.get("userErrors", [])
    if comp_errors:
        print(f"Complete User Errors: {comp_errors}")
        return None

    completed_order = comp_data.get("draftOrder", {}).get("order", {})
    print(f"Successfully Created Payment-Pending Order: {completed_order}")
    return completed_order


if __name__ == "__main__":
    create_pending_payment_order()
