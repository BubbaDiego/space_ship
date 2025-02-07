#!/usr/bin/env python3.10
import requests

def main():

    import requests
    #r = requests.post("https://www.deadlypanda.com/update_jupiter")
    r = requests.post("https://www.deadlypanda.com/update_jupiter", verify=False)
    print(r.status_code, r.text)

if __name__ == "__main__":
    main()
