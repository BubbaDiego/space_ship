from twilio.rest import Client

account_sid = "ACb606788ada5dccbfeeebed0f440099b3"
auth_token = "d3e5a69821872a3ff9d5c90a5c59eafa"

try:
    client = Client(account_sid, auth_token)
    # Fetch your account details as a simple test
    account = client.api.accounts(account_sid).fetch()
    print("Account friendly name:", account.friendly_name)
except Exception as e:
    print("Error:", e)
