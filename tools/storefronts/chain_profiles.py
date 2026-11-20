"""Additional chain profiles grouped by map tags and storefront architecture.

Colors describe the sign background and lettering. Geometry uses the mapped footprint.
"""

GROUPS = [
    ("dining", "amenity", {"fast_food", "restaurant", "cafe"}, [
        ("In-N-Out Burger", "#ad211c", "#ffdf46"),
        ("Whataburger", "#b94813", "#ffffff"),
        ("Culver's", "#164887", "#ffffff"),
        ("Five Guys", "#a82028", "#ffffff"),
        ("Shake Shack", "#263326", "#a6cf65"),
        ("Raising Cane's", "#aa1e31", "#ffffff"),
        ("Jack in the Box", "#b22030", "#ffffff"),
        ("Carl's Jr.", "#292825", "#f8cb34"),
        ("Hardee's", "#282724", "#f6c53b"),
        ("Dairy Queen", "#b22230", "#ffffff"),
        ("Chipotle", "#69261d", "#fff3df"),
        ("QDOBA", "#63422c", "#ffcd66"),
        ("Panda Express", "#ac2028", "#ffffff"),
        ("Wingstop", "#23523c", "#ffffff"),
        ("Buffalo Wild Wings", "#f1c43d", "#222222"),
        ("Panera Bread", "#475e2b", "#fff4da"),
        ("Jersey Mike's Subs", "#17477c", "#ffffff"),
        ("Jimmy John's", "#262326", "#ffffff"),
        ("Firehouse Subs", "#a82020", "#ffedbb"),
        ("Little Caesars", "#ea7226", "#1e1b19"),
        ("Papa Johns", "#a92330", "#ffffff"),
        ("Cracker Barrel", "#ebbd62", "#623520"),
        ("Waffle House", "#ffda44", "#231e1c"),
        ("IHOP", "#1a578d", "#ffffff"),
    ]),
    ("grocery", "shop", {"supermarket"}, [
        ("Whole Foods Market", "#145940", "#ffffff"),
        ("Trader Joe's", "#ab2932", "#fff2dc"),
        ("Food Lion", "#165191", "#ffffff"),
        ("Harris Teeter", "#345c3b", "#ffffff"),
        ("Safeway", "#aa2432", "#ffffff"),
        ("Albertsons", "#24528c", "#ffffff"),
        ("Meijer", "#f4f2ec", "#b32132"),
        ("H-E-B", "#bb2330", "#ffffff"),
        ("Wegmans", "#802e24", "#fff2df"),
        ("Giant Eagle", "#ad2833", "#ffffff"),
        ("Sprouts Farmers Market", "#416439", "#ffffff"),
        ("Lidl", "#254c98", "#ffe052"),
    ]),
    ("discount", "shop", {"department_store", "variety_store", "discount", "clothes"}, [
        ("Dollar Tree", "#28603d", "#ffffff"),
        ("Family Dollar", "#c94226", "#ffffff"),
        ("Five Below", "#15588a", "#ffffff"),
        ("Big Lots", "#ec8229", "#25211f"),
        ("T.J. Maxx", "#ad2430", "#ffffff"),
        ("Marshalls", "#28477e", "#ffffff"),
        ("Ross Dress for Less", "#2e5d99", "#ffffff"),
        ("Burlington", "#a62438", "#ffffff"),
        ("Kohl's", "#442f29", "#ffffff"),
        ("Macy's", "#f5eeee", "#8d1d2b"),
    ]),
    ("warehouse", "shop", {"electronics", "computer"}, [
        ("Best Buy", "#f5d840", "#202128"),
        ("Micro Center", "#252324", "#ffcf52"),
    ]),
    ("warehouse", "shop", {"furniture", "houseware", "interior_decoration"}, [
        ("IKEA", "#1c4991", "#ffdc42"),
        ("HomeGoods", "#9e2937", "#ffffff"),
        ("Ashley", "#332f2b", "#f9ab55"),
    ]),
    ("warehouse", "shop", {"sports", "outdoor", "fishing", "hunting"}, [
        ("Dick's Sporting Goods", "#255444", "#ffffff"),
        ("Academy Sports + Outdoors", "#1e4580", "#ffffff"),
        ("Bass Pro Shops", "#ead47b", "#9c3025"),
        ("Cabela's", "#515138", "#f5db73"),
        ("REI", "#304539", "#ffffff"),
    ]),
    ("specialty", "shop", {"pet", "pet_supply"}, [
        ("PetSmart", "#24518a", "#ffffff"),
        ("Petco", "#244885", "#ffffff"),
    ]),
    ("warehouse", "shop", {"doityourself", "hardware", "agrarian", "trade"}, [
        ("Harbor Freight Tools", "#a5212c", "#ffffff"),
        ("Tractor Supply Co.", "#952630", "#ffffff"),
        ("Menards", "#28653c", "#ffffff"),
    ]),
    ("specialty", "shop", {"car_parts"}, [
        ("AutoZone", "#bb312c", "#ffffff"),
        ("O'Reilly Auto Parts", "#196340", "#ffffff"),
        ("Advance Auto Parts", "#bd262d", "#ffdf5d"),
    ]),
    ("specialty", "shop", {"books", "stationery"}, [
        ("Barnes & Noble", "#385441", "#f6e8c8"),
        ("Staples", "#b42029", "#ffffff"),
        ("Office Depot", "#a8202c", "#ffffff"),
    ]),
    ("specialty", "shop", {"craft", "fabric", "variety_store"}, [
        ("Michaels", "#aa283a", "#ffffff"),
        ("Hobby Lobby", "#bf501f", "#ffffff"),
    ]),
    ("specialty", "shop", {"beauty", "cosmetics", "perfumery"}, [
        ("Ulta Beauty", "#4a3042", "#ffffff"),
    ]),
    ("cinema", "amenity", {"cinema"}, [
        ("AMC Theatres", "#a51d35", "#ffffff"),
        ("Regal Cinemas", "#282d38", "#ffac52"),
        ("Cinemark", "#a32333", "#ffffff"),
    ]),
]

PROFILES = {name: (family, background, lettering)
            for family, _, _, brands in GROUPS for name, background, lettering in brands}

ALIASES = {
    "in-n-out": "In-N-Out Burger", "in n out burger": "In-N-Out Burger",
    "amc": "AMC Theatres", "amc theaters": "AMC Theatres", "regal": "Regal Cinemas",
    "tj maxx": "T.J. Maxx", "t.j.maxx": "T.J. Maxx", "ross": "Ross Dress for Less",
    "harbor freight": "Harbor Freight Tools", "tractor supply company": "Tractor Supply Co.",
    "tractor supply": "Tractor Supply Co.", "dick's": "Dick's Sporting Goods",
    "sprouts": "Sprouts Farmers Market", "chipotle mexican grill": "Chipotle",
    "papa john's": "Papa Johns", "raising cane's chicken fingers": "Raising Cane's",
    "jersey mike's": "Jersey Mike's Subs", "whole foods": "Whole Foods Market",
}
