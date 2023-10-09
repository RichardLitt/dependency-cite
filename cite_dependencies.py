import requests
import re
import os
from datetime import datetime

def get_authors_from_github(repo_path, title, url):
    headers = {
        "Authorization": f"token {os.environ['GITHUB_TOKEN']}",
        "User-Agent": "CitationBot"
    }

    contributors_url = f"https://api.github.com/repos/{repo_path}/contributors"
    response = requests.get(contributors_url, headers=headers)
    contributors = response.json()

    authors = []
    for contributor in contributors:
        username = contributor.get('login')

        # Fetching full name of the user from GitHub
        user_info_url = f"https://api.github.com/users/{username}"
        user_response = requests.get(user_info_url, headers=headers)
        user_data = user_response.json()

        full_name = user_data.get('name') or username

        authors.append(full_name)

    today = datetime.today().strftime('%Y-%m-%d')

    # Fetching the publication year of the version from PyPI
    pypi_url = f"https://pypi.org/pypi/{title}/{version}/json"
    response = requests.get(pypi_url)
    pypi_data = response.json()
    release_date = pypi_data.get('urls', {})[0].get('upload_time', {})
    publication_year = datetime.fromisoformat(release_date).year if release_date else "UNKNOWN"

    return {
        "authors": authors,
        "publication_year": publication_year,
        "title": title,
        "url": url,
        "today": today
    }

def write_bibtex_to_file(data):
    author_string = ' and '.join([f'{{{author}}}' for author in data['authors']])
    bibtex_entry = f"""
@software{{{data['title'].lower()},
  author = {{{author_string}}},
  title = {{{data['title']}}},
  url = {{{data['url']}}},
  version = {{{data['version']}}},
  date = {{{data['publication_year']}}},
}}
"""
    with open('output.bib', 'a') as file:
        file.write(bibtex_entry)

# Parse requirements.txt for package names and versions
with open('requirements.txt', 'r') as file:
    lines = file.readlines()
    packages_and_versions = [tuple(line.strip().split('==')) for line in lines if '==' in line]

# Regex to match GitHub URLs
github_pattern = re.compile(r'https?://github\.com/([\w\-]+/[\w\-]+)')

# For each package, fetch GitHub URL, get authors and write to BibTeX
for package_name, version in packages_and_versions:
    response = requests.get(f'https://pypi.org/pypi/{package_name}/json')
    if response.status_code != 200:
        continue

    data = response.json()
    package_info = data.get('info', {})
    for key, value in package_info.items():
        if isinstance(value, str):
            match = github_pattern.search(value)
            if match:
                repo_path = match.group(1)
                data = get_authors_from_github(repo_path, package_name, match.group(0))
                data['version'] = version  # Adding the version number
                print(f"{', '.join(data['authors'])} ({data['publication_year']}). {data['title']}. Retrieved from {data['url']} on {data['today']}.")
                write_bibtex_to_file(data)
                break
