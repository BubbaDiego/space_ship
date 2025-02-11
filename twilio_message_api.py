import os
from twilio.rest import Client

# Set your Twilio credentials and other configuration as environment variables
os.environ['TWILIO_ACCOUNT_SID'] = 'ACb606788ada5dccbfeeebed0f440099b3'
os.environ['TWILIO_AUTH_TOKEN'] = 'd7cab5fa3dae34e4a1a80741678195c0'
os.environ['TWILIO_FLOW_SID'] = 'FW5b3bf49ee04af4d23a118b613bbc0df2'
os.environ['TWILIO_TO_PHONE'] = '+16199804758'
os.environ['TWILIO_FROM_PHONE'] = '+18336913467'


def trigger_twilio_flow(custom_message):
    """
    Trigger a Twilio Studio Flow execution with a custom message.
    """
    # Retrieve credentials and config from environment variables
    account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
    auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
    flow_sid = os.environ.get('TWILIO_FLOW_SID')
    to_phone = os.environ.get('TWILIO_TO_PHONE')
    from_phone = os.environ.get('TWILIO_FROM_PHONE')

    # Validate that all variables are set
    if not all([account_sid, auth_token, flow_sid, to_phone, from_phone]):
        raise ValueError("One or more Twilio configuration variables are missing.")

    # Initialize the Twilio client
    client = Client(account_sid, auth_token)

    # Pass the custom message as a parameter to your Studio Flow.
    execution = client.studio.v2.flows(flow_sid).executions.create(
        to=to_phone,
        from_=from_phone,
        parameters={"custom_message": custom_message}
    )

    return execution.sid


if __name__ == '__main__':
    try:
        # Customize your message here:
        custom_msg = "Bananas and cream!"
        execution_sid = trigger_twilio_flow(custom_msg)
        print(f"Execution started successfully, SID: {execution_sid}")
    except Exception as e:
        print(f"Error triggering Twilio flow: {e}")
