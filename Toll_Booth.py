import streamlit as st
from streamlit_folium import st_folium
import folium
import pandas as pd
import time
import random

PATHA = [
    [90.422716, 23.825473],
    [90.428467, 23.826572],
    [90.433874, 23.827907],
    [90.439625, 23.82877],
    [90.444174, 23.829791],
    [90.449066, 23.830733],
    [90.453014, 23.831518],
    [90.45722, 23.832618],
    [90.463057, 23.833795],
    [90.468636, 23.835444],
    [90.474043, 23.836386],
    [90.479794, 23.837171],
    [90.486832, 23.837721],
    [90.496616, 23.83725],
    [90.506487, 23.837328],
    [90.511551, 23.837093],
    [90.518847, 23.837407],
    [90.52331, 23.837093],
    [90.526915, 23.834659],
    [90.52906, 23.834423],
    [90.533438, 23.834659],
    [90.538845, 23.834502],
    [90.54039, 23.834502],
    [90.542793, 23.835051],
    [90.545969, 23.836465],
    [90.548973, 23.837093],
]
PATHA = list(map(lambda x: [x[1], x[0]], PATHA))
PATHB = [
    [90.385551, 23.937938],
    [90.390015, 23.926641],
    [90.395508, 23.914401],
    [90.398598, 23.902788],
    [90.400314, 23.892744],
    [90.400658, 23.883012],
    [90.400658, 23.874222],
    [90.400658, 23.862606],
    [90.408211, 23.850674],
    [90.41954, 23.839055],
    [90.4216, 23.826179],
]
PATHB = list(map(lambda x: [x[1], x[0]], PATHB))
PATHC = [
    [90.422974, 23.825237],
    [90.419884, 23.82084],
    [90.410957, 23.816757],
    [90.404778, 23.812046],
    [90.402031, 23.803879],
    [90.401001, 23.792884],
    [90.398941, 23.780946],
    [90.395851, 23.770578],
    [90.394821, 23.762723],
    [90.394478, 23.759267],
]
PATHC = list(map(lambda x: [x[1], x[0]], PATHC))

congested_paths = [
    {
        "path": PATHA,
        "congestion_level": "High",
    },
    {"path": PATHB, "congestion_level": "Medium"},
    {
        "path": PATHC,
        "congestion_level": "Low",
    },
]


def get_congestion_color(congestion_level):
    if congestion_level == "High":
        return "red"
    elif congestion_level == "Medium":
        return "orange"
    elif congestion_level == "Low":
        return "green"
    else:
        return "blue"


def cook_breakfast():
    msg = st.toast("Gathering informations...")
    time.sleep(1)
    msg.toast("Fetching...")
    time.sleep(1)
    msg.toast("Ready!", icon="🎉")


def generate_number_plate():
    # Define the format of the number plate
    letters = [chr(i) for i in range(65, 91)]  # A-Z
    digits = [str(i) for i in range(10)]  # 0-9

    # Generate the number plate
    number_plate = ""
    for _ in range(3):
        number_plate += random.choice(letters)
    number_plate += "-"
    for _ in range(4):
        number_plate += random.choice(digits)

    return number_plate


def yield_data():
    sample = ["PAID", "UNPAID"]
    i = 1
    for l in list(range(0, 500, 1)):
        yield pd.DataFrame(
            {
                "Number Plate": [generate_number_plate()],
                "Payment Status": [random.choice(sample)],
            },
            index=[f"{i}"],
        )
        i += 1
        time.sleep(3)


def simulate_vehicles():
    bus, truck, car = 0, 0, 0
    bus2, truck2, car2 = 0, 0, 0
    congestion = [False, True]
    for _ in list(range(0, 500, 1)):
        yield pd.DataFrame(
            {
                "Incoming": {
                    "Bus": bus,
                    "Truck": truck,
                    "Car": car,
                    "Congestion": random.choice(congestion),
                },
                "Outgoing": {
                    "Bus": bus2,
                    "Truck": truck2,
                    "Car": car2,
                    "Congestion": random.choice(congestion),
                },
            }
        )
        bus += random.choice([0, 1, 2])
        truck += random.choice([0, 1, 2])
        car += random.choice([1, 2, 3])
        bus2 += random.choice([0, 1, 2])
        truck2 += random.choice([0, 1, 2])
        car2 += random.choice([1, 2, 3])
        time.sleep(2)


def main():
    # Title of the app
    st.set_page_config(
        page_title="Traffic Monitoring App",
        layout="wide",
        page_icon="🧊",
        initial_sidebar_state="auto",
    )
    css = """
        [data-testid="stSidebar"] {
            width: 236px !important;
        }
        [data-testid="toastContainer"] {
            position:absolute;
            top: 40px;
        }
        [data-testid="stAppViewBlockContainer"] {
            padding-top: 60px;
            padding-bottom: 60px;
        }
        [data-testid="stButton"] {
            padding-top: 50px;
        }
        [data-testid="stSidebarNav"] {
            position:absolute;
            bottom: 75%;
        }
        [data-testid="stSidebarNavSeparator"] {
            display: none;
        }
        """

    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)
    # Sidebar menu
    ms = st.session_state

    if "themes" not in ms:
        ms.themes = {
            "current_theme": "light",
            "refreshed": True,
            "light": {
                # "theme.base": "dark",
                "theme.backgroundColor": "#f0f0f5",
                "theme.primaryColor": "#6eb52f",
                "theme.secondaryBackgroundColor": "#e0e0ef",
                "theme.textColor": "#262730",
                "theme.font": "sans serif",
                "button_face": "🌜",
            },
            "dark": {
                # "theme.base": "light",
                "theme.backgroundColor": "#002b36",
                "theme.primaryColor": "#d33682",
                "theme.secondaryBackgroundColor": "#586e75",
                "theme.textColor": "#fafafa",
                "theme.font": "sans serif",
                "button_face": "🌞",
            },
        }

    def ChangeTheme():
        previous_theme = ms.themes["current_theme"]
        tdict = (
            ms.themes["light"]
            if ms.themes["current_theme"] == "light"
            else ms.themes["dark"]
        )
        for vkey, vval in tdict.items():
            if vkey.startswith("theme"):
                st._config.set_option(vkey, vval)

        ms.themes["refreshed"] = False
        if previous_theme == "dark":
            ms.themes["current_theme"] = "light"
        elif previous_theme == "light":
            ms.themes["current_theme"] = "dark"

    btn_face = (
        ms.themes["light"]["button_face"]
        if ms.themes["current_theme"] == "light"
        else ms.themes["dark"]["button_face"]
    )

    if ms.themes["refreshed"] == False:
        ms.themes["refreshed"] = True
        st.rerun()

    st.sidebar.image("logo.png", caption="Graaho Technologies", width=130)
    c1, c2, c3 = st.columns((2, 2, 2))
    c1.button(btn_face, on_click=ChangeTheme)
    c2.title("Toll Booth")
    # c3.button(btn_face, on_click=ChangeTheme)
    show_toll_booth_page()


def show_toll_booth_page():
    MAPPER = {
        "Toll Plaza A": {"video": "https://www.youtube.com/embed/ckoAwzsCOxo"},
        "Toll Plaza B": {"video": "https://www.youtube.com/embed/Nd_Ip6QLj7o"},
    }

    col1, col2 = st.columns([3, 1])

    with col1:
        with st.container(height=450):
            if "VIDEO_URL" in st.session_state:
                playlist_id = st.session_state["VIDEO_URL"].split("/")[-1]
                url_style = """
                    <style>
                    .iframe-container {
                        overflow: hidden;
                        width: 100%;
                        height: 430px;
                    }
                    .iframe-container iframe {
                        position: absolute;
                        top: 0;
                        left: 0;
                        width: 100%;
                        height: 430px;
                        padding-bottom: 10px;
                    }
                    </style>
                    """
                url = f"""
                    <div class="iframe-container">
                        <iframe src="{st.session_state["VIDEO_URL"]}?rel=0&amp;&amp;controls=0&amp;showinfo=0&amp;loop=1&autoplay=1&mute=1&playlist={playlist_id}" frameborder="0" allow="autoplay; encrypted-media" allowfullscreen></iframe>
                    </div>
                    """
                url = url_style + url
                st.write(
                    str(url),
                    unsafe_allow_html=True,
                )
            else:
                st.session_state["VIDEO_URL"] = list(MAPPER.values())[0]["video"]

        with st.container(height=270):
            m = folium.Map(
                location=[23.828725716729313, 90.44034752339583], zoom_start=13
            )
            folium.Marker(
                [23.828725716729313, 90.44034752339583], popup="Toll Plaza A"
            ).add_to(m)
            folium.Marker(
                [23.836842165760064, 90.47714944282039], popup="Toll Plaza B"
            ).add_to(m)

            for path_data in congested_paths:
                folium.PolyLine(
                    path_data["path"],
                    color=get_congestion_color(path_data["congestion_level"]),
                    weight=10,
                ).add_to(m)

            st_data = st_folium(m, width=1100, height=240)

    with col2:
        with st.container(height=735):
            placeholder = st.empty()
            if st_data["last_object_clicked_popup"]:
                placeholder.empty()
                st.session_state["VIDEO_URL"] = MAPPER[
                    st_data["last_object_clicked_popup"]
                ]["video"]
                cook_breakfast()
                message = placeholder.chat_message("assistant")
                message.write(
                    f"Real Time Stream for {st_data['last_object_clicked_popup']}"
                )
                message.write_stream(yield_data)
            else:
                placeholder.empty()
                time.sleep(1)
                placeholder.empty()
                message = placeholder.chat_message("assistant")
                message.write(f"Real Time Stream for {list(MAPPER.keys())[0]}")
                message.write_stream(yield_data)


def show_congestion_page():
    MAPPER = {
        "Toll Plaza A": {"video": "https://www.youtube.com/embed/KTkHeOmKV6g"},
        "Toll Plaza B": {"video": "https://www.youtube.com/embed/aTJVDGKHAHU"},
    }

    col1, col2 = st.columns([3, 1])

    with col1:
        with st.container(height=450):
            if "VIDEO_URL" in st.session_state:
                playlist_id = st.session_state["VIDEO_URL"].split("/")[-1]
                url_style = """
                    <style>
                    .iframe-container {
                        overflow: hidden;
                        width: 100%;
                        height: 430px;
                    }
                    .iframe-container iframe {
                        position: absolute;
                        top: 0;
                        left: 0;
                        width: 100%;
                        height: 430px;
                        padding-bottom: 10px;
                    }
                    </style>
                    """
                url = f"""
                    <div class="iframe-container">
                        <iframe src="{st.session_state["VIDEO_URL"]}?rel=0&amp;&amp;controls=0&amp;showinfo=0&amp;loop=1&autoplay=1&mute=1&playlist={playlist_id}" frameborder="0" allow="autoplay; encrypted-media" allowfullscreen></iframe>
                    </div>
                    """
                url = url_style + url
                st.write(
                    str(url),
                    unsafe_allow_html=True,
                )
            else:
                st.session_state["VIDEO_URL"] = list(MAPPER.values())[0]["video"]

        with st.container(height=270):
            m = folium.Map(
                location=[23.828725716729313, 90.44034752339583], zoom_start=13
            )
            folium.Marker(
                [23.828725716729313, 90.44034752339583], popup="Toll Plaza A"
            ).add_to(m)
            folium.Marker(
                [23.836842165760064, 90.47714944282039], popup="Toll Plaza B"
            ).add_to(m)

            for path_data in congested_paths:
                folium.PolyLine(
                    path_data["path"],
                    color=get_congestion_color(path_data["congestion_level"]),
                    weight=10,
                ).add_to(m)

            st_data = st_folium(m, width=1050, height=240)

    with col2:
        with st.container(height=735):
            if st_data["last_object_clicked_popup"]:
                st.session_state["VIDEO_URL"] = MAPPER[
                    st_data["last_object_clicked_popup"]
                ]["video"]
                cook_breakfast()
                message = st.chat_message("assistant")
                message.write(
                    f"Real Time Stream for {st_data['last_object_clicked_popup']}"
                )
                message2 = st.chat_message("user")
                message2.write_stream(simulate_vehicles)
            else:
                message = st.chat_message("assistant")
                message.write(f"Real Time Stream for {list(MAPPER.keys())[0]}")
                message2 = st.chat_message("user")
                message2.write_stream(simulate_vehicles)


if __name__ == "__main__":
    main()
