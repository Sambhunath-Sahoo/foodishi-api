"""Reference data for the seeder.

Hand-written rather than generated from a faker: a menu of "Product 17" makes
every screen and every support answer meaningless to look at.
"""

CUISINES = [
    ("North Indian", "north-indian"),
    ("South Indian", "south-indian"),
    ("Chinese", "chinese"),
    ("Biryani", "biryani"),
    ("Pizza", "pizza"),
    ("Desserts", "desserts"),
    ("Beverages", "beverages"),
    ("Street Food", "street-food"),
]

# (city, area, latitude, longitude) — real neighbourhoods so distances behave.
LOCATIONS = [
    ("Bengaluru", "Koramangala", 12.9352, 77.6245),
    ("Bengaluru", "Indiranagar", 12.9784, 77.6408),
    ("Bengaluru", "Jayanagar", 12.9250, 77.5938),
    ("Bengaluru", "HSR Layout", 12.9116, 77.6474),
    ("Bengaluru", "Whitefield", 12.9698, 77.7500),
    ("Hyderabad", "Banjara Hills", 17.4126, 78.4392),
    ("Hyderabad", "Gachibowli", 17.4401, 78.3489),
    ("Hyderabad", "Jubilee Hills", 17.4326, 78.4071),
    ("Hyderabad", "Madhapur", 17.4483, 78.3915),
    ("Pune", "Koregaon Park", 18.5362, 73.8939),
    ("Pune", "Baner", 18.5590, 73.7868),
    ("Pune", "Viman Nagar", 18.5679, 73.9143),
]

#: FIVE restaurants, chosen so the five still cover all eight cuisines above.
#:
#: It was twenty-five. A smaller catalog is faster to seed and far easier to hold
#: in your head when checking a screen, but it must not cost coverage: a cuisine
#: with no restaurant behind it means the cuisine filter, the cuisine-scoped
#: coupon and the "no results" empty state all go unexercised. So the five are
#: picked for their cuisine PAIRS rather than by taking the first five:
#:
#:   Tandoori Nights   north-indian + biryani
#:   Dakshin Diaries   south-indian
#:   Wok This Way      chinese
#:   Chai Point Cafe   beverages + street-food
#:   Cheese Republic   pizza + desserts
#:
#: Adding a sixth is free; dropping one of these five leaves a cuisine empty.
RESTAURANTS = [
    ("Tandoori Nights", ["north-indian", "biryani"]),
    ("Dakshin Diaries", ["south-indian"]),
    ("Wok This Way", ["chinese"]),
    ("Chai Point Cafe", ["beverages", "street-food"]),
    ("Cheese Republic", ["pizza", "desserts"]),
]

# cuisine slug -> (category name, [(dish, min price, max price, is_veg, spice)])
MENUS = {
    "north-indian": [
        ("Starters", [
            ("Paneer Tikka", 220, 320, True, "medium"),
            ("Hara Bhara Kebab", 190, 260, True, "mild"),
            ("Chicken Malai Tikka", 280, 400, False, "mild"),
            ("Tandoori Chicken (Half)", 300, 420, False, "hot"),
            ("Amritsari Fish Fry", 340, 460, False, "medium"),
        ]),
        ("Mains", [
            ("Dal Makhani", 240, 320, True, "mild"),
            ("Paneer Butter Masala", 260, 360, True, "mild"),
            ("Kadai Paneer", 260, 350, True, "medium"),
            ("Butter Chicken", 320, 450, False, "mild"),
            ("Rogan Josh", 340, 470, False, "hot"),
            ("Chole Bhature", 180, 250, True, "medium"),
        ]),
        ("Breads", [
            ("Butter Naan", 50, 80, True, "none"),
            ("Garlic Naan", 60, 95, True, "none"),
            ("Laccha Paratha", 55, 85, True, "none"),
            ("Tandoori Roti", 30, 50, True, "none"),
        ]),
    ],
    "south-indian": [
        ("Tiffin", [
            ("Masala Dosa", 110, 170, True, "mild"),
            ("Plain Dosa", 80, 130, True, "none"),
            ("Rava Dosa", 120, 180, True, "mild"),
            ("Idli (2 pcs)", 60, 100, True, "none"),
            ("Medu Vada (2 pcs)", 70, 110, True, "mild"),
            ("Pongal", 100, 150, True, "mild"),
        ]),
        ("Meals", [
            ("South Indian Thali", 200, 300, True, "medium"),
            ("Curd Rice", 90, 140, True, "none"),
            ("Lemon Rice", 90, 140, True, "mild"),
            ("Bisi Bele Bath", 130, 190, True, "medium"),
        ]),
        ("Sides", [
            ("Sambar Bowl", 50, 80, True, "medium"),
            ("Coconut Chutney", 30, 50, True, "mild"),
            ("Ghee Podi", 40, 70, True, "hot"),
        ]),
    ],
    "chinese": [
        ("Starters", [
            ("Veg Manchurian Dry", 190, 260, True, "medium"),
            ("Chilli Paneer", 210, 290, True, "hot"),
            ("Chicken Lollipop", 240, 340, False, "hot"),
            ("Crispy Corn", 180, 240, True, "mild"),
        ]),
        ("Noodles & Rice", [
            ("Hakka Noodles", 180, 250, True, "medium"),
            ("Schezwan Fried Rice", 190, 270, True, "hot"),
            ("Chicken Fried Rice", 220, 310, False, "medium"),
            ("Triple Schezwan Rice", 260, 360, False, "hot"),
        ]),
        ("Mains", [
            ("Kung Pao Chicken", 280, 390, False, "hot"),
            ("Mapo Tofu", 240, 330, True, "hot"),
        ]),
    ],
    "biryani": [
        ("Biryani", [
            ("Hyderabadi Chicken Biryani", 280, 400, False, "hot"),
            ("Mutton Dum Biryani", 380, 520, False, "hot"),
            ("Veg Dum Biryani", 220, 320, True, "medium"),
            ("Egg Biryani", 210, 300, False, "medium"),
            ("Prawn Biryani", 400, 560, False, "hot"),
        ]),
        ("Accompaniments", [
            ("Mirchi Ka Salan", 70, 110, True, "hot"),
            ("Raita", 50, 80, True, "none"),
            ("Double Ka Meetha", 90, 140, True, "none"),
        ]),
    ],
    "pizza": [
        ("Pizzas", [
            ("Margherita", 220, 320, True, "none"),
            ("Farmhouse", 300, 430, True, "mild"),
            ("Peri Peri Chicken", 360, 500, False, "hot"),
            ("Paneer Makhani Pizza", 320, 450, True, "medium"),
            ("Pepperoni", 380, 540, False, "medium"),
        ]),
        ("Sides", [
            ("Garlic Breadsticks", 130, 190, True, "none"),
            ("Cheesy Dip", 40, 70, True, "none"),
            ("Peri Peri Fries", 150, 210, True, "hot"),
        ]),
    ],
    "desserts": [
        ("Indian Sweets", [
            ("Gulab Jamun (2 pcs)", 80, 130, True, "none"),
            ("Rasmalai (2 pcs)", 110, 170, True, "none"),
            ("Gajar Ka Halwa", 120, 180, True, "none"),
        ]),
        ("Bakery", [
            ("Chocolate Truffle Slice", 150, 230, True, "none"),
            ("New York Cheesecake", 220, 320, True, "none"),
            ("Tiramisu Jar", 200, 300, True, "none"),
            ("Brownie with Ice Cream", 180, 260, True, "none"),
        ]),
    ],
    "beverages": [
        ("Hot", [
            ("Masala Chai", 40, 70, True, "mild"),
            ("Filter Coffee", 50, 90, True, "none"),
            ("Cappuccino", 120, 190, True, "none"),
        ]),
        ("Cold", [
            ("Sweet Lassi", 90, 140, True, "none"),
            ("Cold Coffee", 130, 200, True, "none"),
            ("Fresh Lime Soda", 60, 100, True, "none"),
            ("Mango Shake", 120, 180, True, "none"),
        ]),
    ],
    "street-food": [
        ("Chaat", [
            ("Pani Puri (6 pcs)", 60, 100, True, "hot"),
            ("Bhel Puri", 70, 110, True, "medium"),
            ("Dahi Puri", 80, 120, True, "mild"),
            ("Samosa Chaat", 90, 140, True, "medium"),
        ]),
        ("Rolls & Pav", [
            ("Vada Pav", 40, 70, True, "medium"),
            ("Misal Pav", 110, 170, True, "hot"),
            ("Chicken Kathi Roll", 160, 240, False, "medium"),
            ("Paneer Kathi Roll", 140, 210, True, "medium"),
        ]),
    ],
}

FIRST_NAMES = [
    "Aarav", "Ananya", "Rohan", "Priya", "Vikram", "Sneha", "Karthik", "Divya",
    "Arjun", "Meera", "Siddharth", "Kavya", "Rahul", "Nisha", "Aditya", "Pooja",
    "Manish", "Shreya", "Nikhil", "Tanvi", "Harsh", "Ishita", "Varun", "Lakshmi",
]
LAST_NAMES = [
    "Sharma", "Reddy", "Iyer", "Patel", "Nair", "Gupta", "Rao", "Menon",
    "Kulkarni", "Desai", "Joshi", "Pillai", "Banerjee", "Chauhan", "Shetty",
]

ADDRESS_LABELS = ["Home", "Work", "Parents", "Hostel"]
VEHICLES = ["bike", "scooter", "bicycle"]
