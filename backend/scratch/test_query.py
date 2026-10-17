import os, httpx
from dotenv import load_dotenv
load_dotenv()

domain = os.getenv('SHOPIFY_SHOP_DOMAIN')
token = os.getenv('SHOPIFY_ACCESS_TOKEN')
version = os.getenv('SHOPIFY_API_VERSION', '2024-04')

url = f'https://{domain}/admin/api/{version}/graphql.json'
headers = {'Content-Type': 'application/json', 'X-Shopify-Access-Token': token}

query = """
query GetOrderBySearch($searchQuery: String!) {
  orders(first: 1, query: $searchQuery) {
    nodes {
      id
      name
      createdAt
      displayFinancialStatus
      displayFulfillmentStatus
      totalPriceSet {
        shopMoney {
          amount
          currencyCode
        }
      }
      customer {
        id
        email
        firstName
        lastName
      }
      lineItems(first: 20) {
        nodes {
          id
          title
          quantity
          originalUnitPriceSet {
            shopMoney {
              amount
            }
          }
        }
      }
    }
  }
}
"""

resp = httpx.post(url, json={'query': query, 'variables': {'searchQuery': 'name:#1001 OR name:1001'}}, headers=headers, timeout=10.0)
print('HTTP status:', resp.status_code)
print('Response JSON:', resp.json())
