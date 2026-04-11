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
    Returns a list of 15-minute price slots with parsed time_start as naive local datetime.
    """
    prices = []

    year = date.strftime("%Y")
    month = date.strftime("%m")
    day = date.strftime("%d")

    next_date = date + timedelta(days=1)
    next_date_year = next_date.strftime("%Y")
    next_date_month = next_date.strftime("%m")
    next_date_day = next_date.strftime("%d")

    url = f"{PRICE_BASE_URL}/{year}/{month}-{day}_{PRICE_ZONE}.json"
    print("Fetch electricity prices for today")

    response = requests.get(url)
    if response.status_code == 200:

        data = response.json()
        for price in data:
            slot_start = datetime.fromisoformat(price["time_start"]).replace(tzinfo=None)

            if slot_start >= date.replace(minute=0, second=0, microsecond=0):
                prices.append(
                    {
                        "time_start": slot_start,
                        "price": price["SEK_per_kWh"]
                    }
                )

    current_hour = int(date.strftime("%H"))
    if current_hour > 13:
        print("Fetch electricity prices for tomorrow")
        url = f"{PRICE_BASE_URL}/{next_date_year}/{next_date_month}-{next_date_day}_{PRICE_ZONE}.json"

        response = requests.get(url)
        if response.status_code == 200:

            data = response.json()
            for price in data:
                slot_start = datetime.fromisoformat(price["time_start"]).replace(tzinfo=None)
                prices.append(
                    {
                        "time_start": slot_start,
                        "price": price["SEK_per_kWh"]
                    }
                )

    return prices

def calculate_slots_to_next_departure(date):
    """
    Calculate number of 15-minute slots from a given datetime until next departure
    """

    hour = int(date.strftime("%H"))
    minute = int(date.strftime("%M"))

    next_departure_hours = DEPARTURE_HOUR - hour

    # check if departure is next day
    if hour > DEPARTURE_HOUR:
        next_departure_hours = DEPARTURE_HOUR + 24 - hour

    # Convert to 15-minute slots, subtract partial hour slots already passed
    slots = next_departure_hours * 4 - (minute // 15)

    return slots

def calculate_slots_needed_to_charge():

    slots_needed = 0

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
            return slots_needed

        percent_to_charge = EV_CHARGE_LIMIT_PERCENT-battery_percent

        kwh_to_charge = EV_BATTERY_CAPACITY_KWH / 100.0 * percent_to_charge

        hours_to_charge = kwh_to_charge/EV_CHARGER_SPEED_KW

        # Convert hours to 15-minute slots, round up
        slots_needed = math.ceil(hours_to_charge * 4)

    return slots_needed

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

def get_battery_state():

    state = ""

    # Fetch smart charing state
    url = f"{HA_BASE_URL}/states/{HA_EV_BATTERY_ENTITY}"
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

    battery_state = get_battery_state()
    if int(battery_state) >= EV_CHARGE_LIMIT_PERCENT:
        print("Battery fully charged, aborting")
        quit()


    print(f"Current date is: {now}")
    # Check slots between now and next departure
    print(f"Next departure hour is set to: {DEPARTURE_HOUR}")
    slots_available = calculate_slots_to_next_departure(now)
    print(f"Number of 15-min slots to next departure is {slots_available}")

    # Calculate number of 15-minute slots needed to charge the battery
    slots_to_charge = calculate_slots_needed_to_charge()
    print(f"Number of 15-min slots needed to charge is: {slots_to_charge}")

    if slots_available <= slots_to_charge:
        print("Too few slots available to smart charge, charging now")
        if charging_state != "charging":
            toggle_charging("ON")

    else:
        prices = fetch_electricity_prices_from_date(now)

        # Determine departure datetime
        departure = now.replace(hour=DEPARTURE_HOUR, minute=0, second=0, microsecond=0)
        if current_hour >= DEPARTURE_HOUR:
            departure += timedelta(days=1)

        prices_to_next_departure = [price for price in prices if price["time_start"] < departure]

        sorted_prices = sorted(prices_to_next_departure, key=lambda x: x["price"])

        # Find current 15-minute slot start
        current_slot_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)

        if any(d["time_start"] == current_slot_start for d in sorted_prices[0:slots_to_charge]):
                print("Current slot is cheap, start or continue charging")
                if charging_state != "charging":
                    toggle_charging("ON")
        else:
            print(f"Current slot is not cheap, stop charging. Charging schedule is: {sorted_prices} ")
            if charging_state == "charging":
                toggle_charging("OFF")
    
    print("** EV Smart charge run ended **")
if __name__ == "__main__":
    main()