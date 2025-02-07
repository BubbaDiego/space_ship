import os
import requests


def main():
    # Build an absolute path relative to this script's directory
    base_dir = os.path.dirname(os.path.abspath(__file__))
    ca_bundle = os.path.join(base_dir, "certs", "ca_bundle.pem")

    if not os.path.exists(ca_bundle):
        print("Certificate bundle not found:", ca_bundle)
        return

    # Make the request using the CA bundle
    r = requests.post("https://www.deadlypanda.com/update_jupiter", verify=ca_bundle)
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
