from __future__ import annotations

import random

DAD_JOKES = [
    "Why don't skeletons fight each other? They don't have the guts.",
    "I used to play piano by ear, but now I use my hands.",
    "Why don't scientists trust atoms? Because they make up everything.",
    "I'm reading a book about anti-gravity. It's impossible to put down.",
    "Did you hear about the claustrophobic astronaut? He just needed a little space.",
    "Why did the scarecrow win an award? Because he was outstanding in his field.",
    "I only know 25 letters of the alphabet. I don't know y.",
    "What do you call fake spaghetti? An impasta.",
    "How does a penguin build its house? Igloos it together.",
    "Why did the bicycle fall over? Because it was two-tired.",
    "I told my wife she was drawing her eyebrows too high. She looked surprised.",
    "What do you call cheese that isn't yours? Nacho cheese.",
    "I'm on a seafood diet. I see food and I eat it.",
    "Why can't you give Elsa a balloon? Because she will let it go.",
    "What did the ocean say to the beach? Nothing, it just waved.",
    "I invented a new word: plagiarism.",
    "Did you hear about the guy who invented Lifesavers? They say he made a mint.",
    "Why did the golfer bring two pairs of pants? In case he got a hole in one.",
    "What do you call a fish with no eyes? A fsh.",
    "How do you organize a space party? You planet.",
    "Why don't eggs tell jokes? They'd crack each other up.",
    "What's orange and sounds like a parrot? A carrot.",
    "I would tell you a chemistry joke, but I know I wouldn't get a reaction.",
    "Why was six afraid of seven? Because seven eight nine.",
    "What did one wall say to the other wall? I'll meet you at the corner.",
    "How does the moon cut his hair? Eclipse it.",
    "What do you call a can opener that doesn't work? A can't opener.",
    "Why did the coffee file a police report? It got mugged.",
    "What's brown and sticky? A stick.",
    "I used to be a banker, but I lost interest.",
    "Why did the math book look sad? Because it had too many problems.",
    "What do you call a bear with no teeth? A gummy bear.",
    "Why don't oysters donate to charity? Because they're shellfish.",
    "I'm afraid for the calendar. Its days are numbered.",
    "What do you call a dinosaur with an extensive vocabulary? A thesaurus.",
    "Why did the picture go to jail? Because it was framed.",
    "What do you get when you cross a snowman and a vampire? Frostbite.",
    "How do you make a tissue dance? You put a little boogie in it.",
    "What did the janitor say when he jumped out of the closet? Supplies!",
    "Why did the tomato turn red? Because it saw the salad dressing.",
]


def random_dad_joke() -> str:
    return random.choice(DAD_JOKES)
