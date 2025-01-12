from flask import Flask, render_template, request, jsonify, make_response
from flask_caching import Cache, CachedResponse
from urllib.parse import unquote
import json
import unicodedata
import re
import csv
import os
import requests

app = Flask(__name__)

cache = Cache()
cache.init_app(app, config={ "CACHE_TYPE": "SimpleCache" })

# Load the substances from JSON with UTF-8 encoding
with open('data/final_updated_drugs.json', encoding='utf-8') as f:
    substances = json.load(f)

# Add regex_match func to Jinja
def regex_match(value, pattern="", flags=0):
  return re.match(pattern, value, flags)

app.jinja_env.filters["regex_match"] = regex_match

# Function to slugify strings for URL-friendly names
def slugify(value):
    """
    Converts a string to a URL-friendly slug.
    """
    value = str(value)
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^\w\s-]', '', value).strip().lower()
    value = re.sub(r'[-\s]+', '-', value)
    return value

# Make slugify available in templates
app.jinja_env.globals.update(slugify=slugify)

# Create a mapping from slugs to substance names
slug_to_substance_name = {}
for substance_name, details in substances.items():
    slug = slugify(substance_name)
    slug_to_substance_name[slug] = substance_name
    # Include aliases in the slug mapping
    for alias in details.get('aliases', []):
        alias_slug = slugify(alias)
        slug_to_substance_name[alias_slug] = substance_name

# Helper function to get all unique categories and apply the necessary transformations
def get_all_categories(substances):
    categories = set()
    for substance in substances.values():
        for category in substance.get('categories', []):
            if category.lower() not in ['inactive', 'tentative', 'habit-forming', 'common', 'ssri']:
                categories.add(category.capitalize())
    return sorted(categories)

# Substance Colors
category_colors = {
    'psychedelic': '#FFB6C1',
    'dissociative': '#87CEFA',
    'stimulant': '#FFD700',
    'research-chemical': '#98FB98',
    'empathogen': '#FF69B4',
    'habit-forming': '#FFA07A',
    'opioid': '#BA55D3',
    'depressant': '#ADD8E6',
    'hallucinogen': '#FF6347',
    'entactogen': '#20B2AA',
    'deliant': '#FFFFE0',
    'antidepressant': '#FF4500',
    'sedative': '#9370DB',
    'nootropic': '#AFEEEE',
    'disassociative': '#FF7F50',
    'barbiturate': '#F08080',
    'benzodiazepine': '#FFDEAD',
    'deliriant': '#FFE4E1',
    'supplement': '#98FB98',
}

# Clean data by removing or replacing None values
def clean_data(data):
    """
    Recursively clean the data to remove None or undefined values.
    """
    if isinstance(data, dict):
        return {k: clean_data(v) for k, v in data.items() if v is not None}
    elif isinstance(data, list):
        return [clean_data(v) for v in data if v is not None]
    return data

# Route for the home page
@app.route('/')
def home():
    categories = get_all_categories(substances)
    return render_template('index.html', categories=categories, category_colors=category_colors)

def rank_to_display_string(rank: int) -> str:
    emoji = ''
    if rank == 1:
        emoji = '🥇 '
    elif rank == 2:
        emoji = '🥈 '
    elif rank == 3:
        emoji = '🥉 '
    return f'{emoji}{rank}'

# Route for the leaderboard
@app.route('/leaderboard')
@cache.cached() # one day timeout
def leaderboard():
    try:
        # setup request prerequisites
        auth_token= os.environ['GITHUB_AUTH_TOKEN'] # get auth token from environment
        headers = {
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {auth_token}',
            'X-Github-Api-Version': '2022-11-28'
        }
        app.logger.info("Fetching leaderboard data")
        r = requests.get(
            'https://api.github.com/repos/ded-grl/SubstanceSearch/contributors',
            headers=headers
        )

        if (not r.ok):
            raise RuntimeError(r.content)

        #parse response data
        contribution_data= r.json()
        app.logger.info("Retrieved contribution data: %s", str(contribution_data))

        leaderboard_data = [{
            'rank': rank_to_display_string(index + 1),
            'contributor': contribution['login'],
            'contributions': contribution['contributions']
        } for index, contribution in enumerate(contribution_data)]

        return CachedResponse(
            response=make_response(render_template('leaderboard.html', leaderboard_data=leaderboard_data)),
            timeout = 86400 # one day timeout
        )
    except Exception as e:
        # In case of error, fallback to static cache data in /data/leaderboard.csv
        app.logger.error(f'Failed to fetch contribution data. [{e}]')
        app.logger.error('Using cached leaderboard data instead.')

        # Read the leaderboard data from the CSV
        leaderboard_data = []
        with open('data/leaderboard.csv', 'r') as file:
            reader = csv.DictReader(file)
            for index, row in enumerate(reader):
                rank = index + 1
                leaderboard_data.append({
                    'rank': rank_to_display_string(rank),
                    'contributor': row['Contributor'],
                    'contributions': row['Contributions']
                })

        # Pass the data to the template
        return CachedResponse(
            response=make_response(render_template('leaderboard.html', leaderboard_data=leaderboard_data)),
            timeout = 60 # one minute response to retry in case of 500 level upstream errors
        )

# Route for fetching autocomplete suggestions
@app.route('/autocomplete', methods=['GET'])
def autocomplete():
    query = request.args.get('query', '').lower()
    results = substance_trie.search_prefix(query)
    return jsonify(results)

# Route for displaying substance information using slugified names
@app.route('/substance/<path:slug>')
def substance(slug):
    # Decode the slug to handle URL-encoded characters
    decoded_slug = unquote(slug)
    # Lookup the original substance name using the slug
    substance_name = slug_to_substance_name.get(decoded_slug.lower())
    if not substance_name:
        return "Substance not found", 404
    substance_data = substances.get(substance_name)
    if not substance_data:
        return "Substance not found", 404

    # Clean the substance data to remove None values
    cleaned_substance_data = clean_data(substance_data)
    return render_template('substance.html', substance=cleaned_substance_data, category_colors=category_colors)

# Route for displaying substances in a category
@app.route('/category/<path:category_slug>')
def category(category_slug):
    # Decode the slug to handle URL-encoded characters
    decoded_slug = unquote(category_slug).lower()
    # Map of slugified category names to their original form
    category_name_mapping = {}
    for substance in substances.values():
        for category in substance.get('categories', []):
            category_slugified = slugify(category)
            category_name_mapping[category_slugified] = category.capitalize()
    # Get the original category name
    category_name = category_name_mapping.get(decoded_slug)
    if not category_name:
        return "Category not found", 404

    # Filter substances that belong to the category
    filtered_substances = {}
    for substance_name, details in substances.items():
        if any(slugify(cat) == decoded_slug for cat in details.get('categories', [])):
            filtered_substances[substance_name] = details

    if not filtered_substances:
        return "Category not found", 404

    return render_template('category.html', category_name=category_name, substances=filtered_substances, category_colors=category_colors)

class TrieNode:
    def __init__(self):
        self.children = {}
        self.is_end = False
        self.data = None

class Trie:
    def __init__(self):
        self.root = TrieNode()
    
    def insert(self, word, data):
        node = self.root
        for char in word.lower():
            if char not in node.children:
                node.children[char] = TrieNode()
            node = node.children[char]
        node.is_end = True
        node.data = data
    
    def search_prefix(self, prefix):
        node = self.root
        results = []
        
        for char in prefix.lower():
            if char not in node.children:
                return results
            node = node.children[char]
            
        self._collect_words(node, prefix, results)
        return results[:10]
    
    def _collect_words(self, node, prefix, results):
        if node.is_end:
            results.append(node.data)
        
        for char, child in node.children.items():
            self._collect_words(child, prefix + char, results)

substance_trie = Trie()
for substance_name, details in substances.items():
    pretty_name = details.get('pretty_name', 'Unknown')
    aliases = details.get('aliases', [])
    slug = slugify(substance_name)
    
    substance_trie.insert(substance_name, {
        'pretty_name': pretty_name,
        'aliases': aliases,
        'slug': slug
    })
    
    substance_trie.insert(pretty_name, {
        'pretty_name': pretty_name,
        'aliases': aliases,
        'slug': slug
    })
    
    for alias in aliases:
        substance_trie.insert(alias, {
            'pretty_name': pretty_name,
            'aliases': aliases,
            'slug': slug
        })

if __name__ == '__main__':
    app.run(debug=True)
