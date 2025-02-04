#!/usr/bin/env python3.10
import requests

def main():

    import requests
    r = requests.post("https://bubbadiego.pythonanywhere.com/update_jupiter")
    print(r.status_code, r.text)

if __name__ == "__main__":
    main()
