import requests
import math
import os
from dotenv import load_dotenv
from datetime import datetime,timedelta

# Load environment variables from .env file
load_dotenv()

PRICE_ZONE = os.getenv('PRICE_ZONE')
PRICE_BASE_URL = os.getenv('PRICE_BASE_URL')
DEPARTURE_HOUR = int(os.getenv('DEPARTURE_HOUR'))
EV_CHARGER_SPEED_KW = float(os.getenv('EV_CHARGER_SPEED_KW'))
EV_BATTERY_CAPACITY_KWH = float(os.getenv('EV_BATTERY_CAPACITY_KWH'))
EV_CHARGE_LIMIT_PERCENT = int(os.getenv('EV_CHARGE_LIMIT_PERCENT'))

HA_BASE_URL = os.getenv('HA_BASE_URL')
HA_TOKEN = os.getenv('HA_TOKEN')
HA_EV_BATTERY_ENTITY = os.getenv('HA_EV_BATTERY_ENTITY')
HA_EV_CHARGE_SWITCH = os.getenv('HA_EV_CHARGE_SWITCH')
HA_EV_CHARGER_STATE = os.getenv('HA_EV_CHARGER_STATE')
HA_EV_SMART_CHARGING_BOOLEAN = os.getenv('HA_EV_SMART_CHARGING_BOOLEAN')

def fetch_electricity_prices_from_date(date):
    """
    Fetch the electricity prices from the API. Given a date, it fetches the prices for that date and the next day if available.
    :param date: The date from which to fetch the electricity prices.
    :return: The electricity prices for the specified date.
    """
    prices = []

    year = date.strftime("%Y")
    month = date.strftime("%m")
    day = date.strftime("%d")
    hour = date.strftime("%H")

    next_date = date + timedelta(days=1)
    next_date_year = next_date.strftime("%Y")
    next_date_month = next_date.strftime("%m")
    next_date_day = next_date.strftime("%d")

    

    url = f"{PRICE_BASE_URL}/{year}/{month}-{day}_{PRICE_ZONE}.json"
    print("Fetch electricity prices for today")

    response = requests.get(url)
    if response.status_code == 200:

        data = response.json()
        day = 0
        for price in data:
            price_hour = int(price["time_start"][11:13])
            current_hour = int(hour)

            if price_hour >= current_hour:            
                prices.append(
                    {
                        "day":day,
                        "hour": price_hour,
                        "price": price["SEK_per_kWh"]
                    }
                )

    if int(hour) > 13:
        print("Fetch electricity prices for tomorrow")
        url = f"{PRICE_BASE_URL}/{next_date_year}/{next_date_month}-{next_date_day}_{PRICE_ZONE}.json"

        response = requests.get(url)
        if response.status_code == 200:

            data = response.json()
            day = 1
            for price in data:
                prices.append(
                    {
                        "day":day,
                        "hour": int(price["time_start"][11:13]),
                        "price": price["SEK_per_kWh"]
                    }
                )

    return prices

def calculate_hours_to_next_charge(date):
    """
    Calculate number of hours from a given datetime until next departure
    """

    hour = date.strftime("%H")

    next_departure_hours = DEPARTURE_HOUR-int(hour)

    # check if departure is next day
    if int(hour) > DEPARTURE_HOUR:
        next_departure_hours = DEPARTURE_HOUR+24-int(hour)
    
    return next_departure_hours

def calculate_hours_needed_to_charge():

    hours_needed_to_charge = 0

    # Fetch ev battery percentage
    url = f"{HA_BASE_URL}/states/{HA_EV_BATTERY_ENTITY}"
    headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "content-type": "application/json",
        }

    response = requests.get(url,headers=headers)
    if response.status_code == 200:
        data = response.json()
        battery_percent_data = data.get("state")
        battery_percent = int(battery_percent_data)

        if EV_CHARGE_LIMIT_PERCENT <= battery_percent:
            return hours_needed_to_charge
        
        percent_to_charge = EV_CHARGE_LIMIT_PERCENT-battery_percent

        kwh_to_charge = EV_BATTERY_CAPACITY_KWH / 100.0 * percent_to_charge

        hours_to_charge = kwh_to_charge/EV_CHARGER_SPEED_KW
        
        hours_needed_to_charge = math.ceil(hours_to_charge)        
    
    return hours_needed_to_charge

def toggle_charging(to_state):

    base_url = f"{HA_BASE_URL}/services/switch"
    headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "content-type": "application/json",
        }
    post_data = {
            "entity_id": HA_EV_CHARGE_SWITCH
        }

    if to_state == "ON":
        url = f"{base_url}/turn_on"
        response = requests.post(url=url, headers=headers, json=post_data)

        if response.status_code == 200:
            print("EV started charging")
        
        else:
            print(f"Failed to start charging, response status: {response.status_code}")
    
    if to_state == "OFF":
        url = f"{base_url}/turn_off"
        response = requests.post(url=url, headers=headers, json=post_data)

        if response.status_code == 200:
            print("EV stopped charging")
        
        else:
            print(f"Failed to stop charging, response status: {response.status_code}")

    else:
        print(f"Received unknown command for toggle charging: {to_state}")

    return

def smart_charging_enabled():

    enabled = False

    # Fetch smart charing state
    url = f"{HA_BASE_URL}/states/{HA_EV_SMART_CHARGING_BOOLEAN}"
    headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "content-type": "application/json",
        }

    response = requests.get(url,headers=headers)
    if response.status_code == 200:
        data = response.json()
        state = data.get("state")

        if state == "on":
            enabled = True

    return enabled

def get_charging_state():

    state = ""

    # Fetch smart charing state
    url = f"{HA_BASE_URL}/states/{HA_EV_CHARGER_STATE}"
    headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "content-type": "application/json",
        }

    response = requests.get(url,headers=headers)
    if response.status_code == 200:
        data = response.json()
        state = data.get("state")

    return state

def main():
    """
    Main function to execute the script.
    # Scrip to determine based on electricity prices wether to charge the battery on an electric vehicle
    1. Fetch the electricity prices from the API
    2. Check hours between now and next departure
    3. Calculate number of hours needed to charge the battery
    # 4. Determine if the battery should be charged now or later based on prices, available hours and hours to charge
    """

    print("** EV Smart charge run started **")
    # Get the current date and time
    now = datetime.now()
    current_hour = int(now.strftime("%H"))
    
    if not smart_charging_enabled():
        print("Smart charging disabled, aborting")
        quit()

    charging_state = get_charging_state()

    if charging_state == "connect_cable":
        print("Cable not connected, aborted")
        quit()

    print(f"Current date is: {now}")
    # Check hours between now and next departure
    print(f"Next departure hour is set to: {DEPARTURE_HOUR}")
    number_of_hours_available_to_charge = calculate_hours_to_next_charge(now)
    print(f"Number of Hours to next departure is {number_of_hours_available_to_charge}")

    # Calculate number of hours needed to charge the battery
    number_of_hours_to_charge = calculate_hours_needed_to_charge()
    print(f"Number of hours needed to charge is: {number_of_hours_to_charge}")

    if number_of_hours_available_to_charge <= number_of_hours_to_charge:
        print("Too few hours available to smart charge, charging now")
        if charging_state != "charging":
            toggle_charging("ON")

    else:
        prices = fetch_electricity_prices_from_date(now)
        
        day = 1
        if current_hour < DEPARTURE_HOUR:
            day = 0
        
        prices_to_next_departure = [price for price in prices if not (price["hour"] > DEPARTURE_HOUR and price["day"] == day)]
        
        sorted_prices = sorted(prices_to_next_departure, key=lambda x: x["price"])

        if any(d.get("day") == 0 and d.get("hour") == current_hour for d in sorted_prices[0:number_of_hours_to_charge]):
                print("Current hour is cheap, start or continue charging")
                if charging_state != "charging":
                    toggle_charging("ON")
        else:
            print(f"Current hour is not cheap, stop charging. Charging schedule is: {sorted_prices} ")
            if charging_state == "charging":
                toggle_charging("OFF")
    
    print("** EV Smart charge run ended **")
if __name__ == "__main__":
    main()