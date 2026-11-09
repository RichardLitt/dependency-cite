import argparse
import json
import os
import re
import sys
import time
import tomllib
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, unquote

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PACKAGES_API = 'https://packages.ecosyste.ms/api/v1'
RESOLVE_API = 'https://resolve.ecosyste.ms/api/v1'
ZENODO_API = 'https://zenodo.org/api/records'
BATCH_SIZE = 100
POLL_SECONDS = 1
POLL_ATTEMPTS = 120

# The resolver silently returns nothing when given `ecosystem=`, so it always
# needs the registry name.
REGISTRIES = {
    'pypi': 'pypi.org',
    'npm': 'npmjs.org',
    'cargo': 'crates.io',
    'gem': 'rubygems.org',
    'golang': 'proxy.golang.org',
}

BIB_TO_CFF = {
    'article': 'article',
    'inproceedings': 'conference-paper',
    'conference': 'conference-paper',
    'book': 'book',
    'software': 'software',
    'techreport': 'report',
    'phdthesis': 'thesis',
    'mastersthesis': 'thesis',
    'manual': 'manual',
    'misc': 'generic',
}
CFF_TO_BIB = {
    'article': 'article',
    'conference-paper': 'inproceedings',
    'book': 'book',
    'software': 'software',
}
CFF_TYPES = set(BIB_TO_CFF.values()) | {'dataset', 'software-code', 'software-container'}
CFF_AUTHOR_KEYS = ('family-names', 'given-names', 'name-particle', 'name-suffix', 'name', 'alias')
CITATION_FIELDS = ('type', 'title', 'authors', 'year', 'doi', 'url', 'journal', 'source', 'zenodo_version', 'review', 'found_in')

http = requests.Session()
http.headers['User-Agent'] = 'dependency-cite (https://github.com/RichardLitt/dependency-cite)'
http.mount('https://', HTTPAdapter(max_retries=Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 502, 503, 504],
    allowed_methods=None,
    respect_retry_after_header=True,
    raise_on_status=False,
)))


def warn(message):
    print(f'warning: {message}', file=sys.stderr)


# Finding dependencies

def normalize_name(ecosystem, name):
    if ecosystem == 'pypi':
        return re.sub(r'[-_.]+', '-', name).lower()
    return name


def make_dependency(ecosystem, name, version=None):
    return {'ecosystem': ecosystem, 'name': normalize_name(ecosystem, name), 'version': version}


def package_url(dependency):
    return f"pkg:{dependency['ecosystem']}/{quote(dependency['name'], safe='/')}"


def parse_requirement(line):
    line = line.split(' #')[0].split(' --')[0].split(';')[0].rstrip('\\ ').strip()
    if not line or line.startswith(('#', '-')):
        return None
    match = re.match(r'([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)', line)
    if not match:
        return None
    pinned = re.fullmatch(r'===?\s*([^\s,*]+)', match.group(2).strip())
    return make_dependency('pypi', match.group(1), pinned.group(1) if pinned else None)


def read_requirements(path):
    requirements = [parse_requirement(line) for line in path.read_text().splitlines()]
    return [requirement for requirement in requirements if requirement]


def read_pyproject(path):
    config = tomllib.loads(path.read_text())
    requirements = [parse_requirement(line) for line in config.get('project', {}).get('dependencies', [])]
    poetry = config.get('tool', {}).get('poetry', {}).get('dependencies', {})
    requirements += [make_dependency('pypi', name) for name in poetry if name.lower() != 'python']
    return [requirement for requirement in requirements if requirement]


def read_python_lock(path):
    dependencies = []
    for package in tomllib.loads(path.read_text()).get('package', []):
        source = package.get('source', {})
        local = 'editable' in source or 'virtual' in source or 'directory' in source
        if local or source.get('type') in ('directory', 'file'):
            continue
        dependencies.append(make_dependency('pypi', package['name'], package.get('version')))
    return dependencies


def read_package_lock(path):
    # Version 1 lockfiles have no `packages` key; package.json is used instead.
    packages = json.loads(path.read_text()).get('packages', {})
    dependencies = []
    for key, package in packages.items():
        if 'node_modules/' not in key or package.get('link'):
            continue
        name = package.get('name') or key.rsplit('node_modules/', 1)[1]
        dependencies.append(make_dependency('npm', name, package.get('version')))
    return dependencies


def read_package_json(path):
    config = json.loads(path.read_text())
    dependencies = []
    for section in ('dependencies', 'devDependencies'):
        for name, requirement in config.get(section, {}).items():
            if requirement.startswith(('file:', 'link:', 'workspace:')):
                continue
            pinned = re.fullmatch(r'v?\d+\.\d+\.\d+\S*', requirement)
            dependencies.append(make_dependency('npm', name, requirement.lstrip('v') if pinned else None))
    return dependencies


def read_cargo_lock(path):
    packages = tomllib.loads(path.read_text()).get('package', [])
    # Packages with no source are members of the project's own workspace.
    return [make_dependency('cargo', package['name'], package.get('version'))
            for package in packages if package.get('source')]


def read_cargo_toml(path):
    config = tomllib.loads(path.read_text())
    dependencies = []
    for section in ('dependencies', 'dev-dependencies', 'build-dependencies'):
        for name, requirement in config.get(section, {}).items():
            details = requirement if isinstance(requirement, dict) else {'version': requirement}
            if 'path' in details or details.get('workspace'):
                continue
            pinned = re.fullmatch(r'=\s*(\S+)', details.get('version', ''))
            dependencies.append(make_dependency('cargo', details.get('package', name),
                                                pinned.group(1) if pinned else None))
    return dependencies


def read_gemfile_lock(path):
    dependencies = []
    section = None
    for line in path.read_text().splitlines():
        if line and not line.startswith(' '):
            section = line.strip()
        match = re.fullmatch(r'    ([^\s(]+) \(([^)]+)\)', line)
        if match and section == 'GEM':
            dependencies.append(make_dependency('gem', match.group(1), match.group(2)))
    return dependencies


def read_go_mod(path):
    dependencies = []
    in_require = False
    for line in path.read_text().splitlines():
        line = line.split('//')[0].strip()
        if line.startswith('require ('):
            in_require = True
            continue
        if line == ')':
            in_require = False
            continue
        match = re.fullmatch(r'(?:require\s+)?(\S+)\s+v(\S+)', line)
        if match and (in_require or line.startswith('require')):
            dependencies.append(make_dependency('golang', match.group(1), match.group(2).replace('+incompatible', '')))
    return dependencies


def parse_purl(purl):
    match = re.match(r'pkg:([^/]+)/([^@?#]+)(?:@([^?#]+))?', purl)
    if not match or match.group(1) == 'github':
        return None
    version = unquote(match.group(3)) if match.group(3) else None
    return make_dependency(match.group(1), unquote(match.group(2)), version)


def read_sbom(path):
    sbom = json.loads(Path(path).read_text())
    purls = [component.get('purl') for component in sbom.get('components', [])]
    for package in sbom.get('packages', []):
        purls += [reference.get('referenceLocator') for reference in package.get('externalRefs', [])
                  if reference.get('referenceType') == 'purl']
    dependencies = [parse_purl(purl) for purl in purls if purl]
    return [dependency for dependency in dependencies if dependency]


# Lockfiles already list the whole tree, so anything else is expanded by the resolver.
LOCKFILES = {
    'uv.lock': ('pypi', read_python_lock),
    'poetry.lock': ('pypi', read_python_lock),
    'package-lock.json': ('npm', read_package_lock),
    'Cargo.lock': ('cargo', read_cargo_lock),
    'Gemfile.lock': ('gem', read_gemfile_lock),
    'go.mod': ('golang', read_go_mod),
}
MANIFESTS = {
    'requirements.txt': ('pypi', read_requirements),
    'pyproject.toml': ('pypi', read_pyproject),
    'package.json': ('npm', read_package_json),
    'Cargo.toml': ('cargo', read_cargo_toml),
}


def merge_dependencies(found, dependencies):
    for dependency in dependencies:
        key = (dependency['ecosystem'], dependency['name'])
        if key not in found or not found[key]['version']:
            found[key] = dependency


def run_resolve_job(dependency):
    """Returns the {name: version} map of everything the dependency pulls in."""
    params = {'package_name': dependency['name'], 'registry': REGISTRIES[dependency['ecosystem']]}
    # The resolver rejects ranges, so only exact versions can be passed along.
    if dependency['version']:
        params['version'] = dependency['version']
    response = http.post(f'{RESOLVE_API}/jobs', params=params, allow_redirects=False, timeout=30)
    if not response.is_redirect:
        response.raise_for_status()
        raise ValueError('the resolver did not start a job')
    job_url = response.headers['location']
    for _ in range(POLL_ATTEMPTS):
        response = http.get(job_url, timeout=30)
        response.raise_for_status()
        job = response.json()
        if job['status'] == 'complete':
            return job['results']
        if job['status'] == 'error':
            raise ValueError(job['results'].get('error', 'unknown resolver error'))
        time.sleep(POLL_SECONDS)
    raise ValueError('timed out waiting for the resolver')


def resolve_dependency(dependency):
    try:
        return run_resolve_job(dependency)
    except ValueError:
        if not dependency['version']:
            raise
    # The pinned version may be unknown to the resolver; the latest is still informative.
    return run_resolve_job({**dependency, 'version': None})


def resolve_transitive(dependencies, problems):
    resolved = []
    for dependency in dependencies:
        if dependency['ecosystem'] not in REGISTRIES:
            problems.append(f"cannot resolve transitive dependencies of {dependency['name']}")
            continue
        try:
            results = resolve_dependency(dependency)
        except (requests.RequestException, ValueError, KeyError) as error:
            problems.append(f"could not resolve transitive dependencies of {dependency['name']}: {error}")
            continue
        resolved += [make_dependency(dependency['ecosystem'], name, version) for name, version in results.items()]
    return resolved


def find_dependencies(project, sbom, problems):
    if sbom:
        found = {}
        merge_dependencies(found, read_sbom(sbom))
        return list(found.values())

    found = {}
    locked = set()
    for filename, (ecosystem, reader) in LOCKFILES.items():
        if (project / filename).exists():
            dependencies = reader(project / filename)
            if dependencies:
                locked.add(ecosystem)
                merge_dependencies(found, dependencies)

    unlocked = {}
    for filename, (ecosystem, reader) in MANIFESTS.items():
        if ecosystem not in locked and (project / filename).exists():
            merge_dependencies(unlocked, reader(project / filename))
    merge_dependencies(found, unlocked.values())
    merge_dependencies(found, resolve_transitive(list(unlocked.values()), problems))
    return list(found.values())


# Reading citations

def clean(value):
    return re.sub(r'\s+', ' ', str(value).replace('{', '').replace('}', '')).strip()


def normalize_url(url):
    url = str(url).strip().lower()
    url = re.sub(r'^(git\+)?(https?|ssh|git)://', '', url)
    url = re.sub(r'^git@([^:/]+)[:/]', r'\1/', url)
    url = re.sub(r'^www\.', '', url)
    url = re.sub(r'\.git$', '', url.split('#')[0].split('?')[0].rstrip('/'))
    match = re.match(r'github\.com/[^/]+/[^/]+', url)
    return match.group(0) if match else url


def normalize_doi(doi):
    match = re.search(r'10\.\d{4,9}/\S+', str(doi))
    return match.group(0).lower().rstrip('.,;') if match else None


def normalize_title(title):
    return re.sub(r'[^a-z0-9]', '', clean(title).lower())


def identifiers_of(*records):
    """Collects everything that could link a citation to a reference."""
    dois, urls, titles = set(), set(), set()
    for record in records:
        found_dois = [record.get('doi')]
        found_dois += [item.get('value') for item in record.get('identifiers') or []
                       if isinstance(item, dict) and item.get('type') == 'doi']
        dois.update(normalize_doi(doi) for doi in found_dois if doi)
        found_urls = [record.get(key) for key in ('repository-code', 'repository', 'url')]
        found_urls += [item.get('value') for item in record.get('identifiers') or []
                       if isinstance(item, dict) and item.get('type') == 'url']
        urls.update(normalize_url(url) for url in found_urls if url)
        if record.get('title'):
            titles.add(normalize_title(record['title']))
            titles.add(normalize_title(str(record['title']).split(':')[0]))
    return {'dois': dois - {None}, 'urls': urls - {''}, 'titles': titles - {''}}


def year_of(record):
    for key in ('year', 'date-released', 'date-published'):
        match = re.match(r'\d{4}', str(record.get(key, '')))
        if match:
            return int(match.group(0))
    return None


def clean_cff_authors(authors):
    cleaned = []
    for author in authors if isinstance(authors, list) else []:
        if isinstance(author, dict):
            fields = {key: str(author[key]) for key in CFF_AUTHOR_KEYS if author.get(key)}
            if fields:
                cleaned.append(fields)
    return cleaned


def citation_from_cff(text):
    try:
        cff = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(cff, dict):
        return None
    preferred = cff.get('preferred-citation')
    entry = preferred if isinstance(preferred, dict) and preferred.get('title') else cff
    if not entry.get('title'):
        return None
    return {
        'type': entry.get('type') if entry.get('type') in CFF_TYPES else 'software',
        'title': clean(entry['title']),
        'authors': clean_cff_authors(entry.get('authors')),
        'year': year_of(entry),
        'doi': normalize_doi(entry.get('doi') or cff.get('doi') or ''),
        'url': entry.get('url') or cff.get('url'),
        'journal': clean(entry['journal']) if entry.get('journal') else None,
        'source': 'CITATION.cff',
        **identifiers_of(cff, entry),
    }


def read_bibtex_value(body, position):
    if body[position] == '"':
        end = position + 1
        while end < len(body) and body[end] != '"':
            end += 1
        return body[position + 1:end], end + 1
    if body[position] == '{':
        depth, end = 0, position
        while end < len(body):
            depth += (body[end] == '{') - (body[end] == '}')
            end += 1
            if depth == 0:
                break
        return body[position + 1:end - 1], end
    match = re.compile(r'[^,\s}]*').match(body, position)
    return match.group(0), match.end()


def parse_bibtex(text):
    entries = []
    pattern = re.compile(r'@(\w+)\s*\{')
    match = pattern.search(text)
    while match:
        depth, end = 1, match.end()
        while end < len(text) and depth:
            depth += (text[end] == '{') - (text[end] == '}')
            end += 1
        kind = match.group(1).lower()
        if kind not in ('comment', 'string', 'preamble'):
            body = text[match.end():end - 1]
            fields = {}
            start = body.find(',') + 1
            field = re.compile(r'\s*,?\s*([\w-]+)\s*=\s*')
            found = field.match(body, start) if start else None
            while found and found.end() < len(body):
                value, position = read_bibtex_value(body, found.end())
                fields[found.group(1).lower()] = value
                found = field.match(body, position)
            entries.append({'kind': kind, 'fields': fields})
        match = pattern.search(text, end)
    return entries


def bibtex_authors(value):
    authors = []
    for name in re.split(r'\s+and\s+', value.strip()):
        name = name.strip()
        if name.lower() == 'others' or not name:
            continue
        if name.startswith('{') and name.endswith('}'):
            authors.append({'name': clean(name)})
        elif ',' in name:
            family, given = name.split(',', 1)
            authors.append({'family-names': clean(family), 'given-names': clean(given)})
        elif ' ' in clean(name):
            given, family = clean(name).rsplit(' ', 1)
            authors.append({'family-names': family, 'given-names': given})
        else:
            authors.append({'name': clean(name)})
    return authors


def bibtex_record(fields):
    """Reshapes BibTeX fields so identifiers_of can read them like CFF."""
    urls = re.findall(r'https?://[^\s{}]+', ' '.join(fields.get(key, '') for key in ('url', 'howpublished', 'note')))
    return {
        'title': fields.get('title'),
        'doi': fields.get('doi'),
        'identifiers': [{'type': 'url', 'value': url} for url in urls],
    }


def citation_from_bibtex(text):
    for entry in parse_bibtex(text):
        fields = entry['fields']
        if fields.get('title'):
            record = bibtex_record(fields)
            return {
                'type': BIB_TO_CFF.get(entry['kind'], 'generic'),
                'title': clean(fields['title']),
                'authors': bibtex_authors(fields.get('author', '')),
                'year': year_of({'year': fields.get('year') or fields.get('date', '')}),
                'doi': normalize_doi(fields.get('doi', '')),
                'url': clean(fields['url']) if fields.get('url') else None,
                'journal': clean(fields.get('journal') or fields.get('journaltitle') or '') or None,
                'source': 'CITATION.bib',
                **identifiers_of(record),
            }
    return None


def raw_file_url(repository_url, path):
    match = re.match(r'https://(github\.com|gitlab\.com)/(.+?)(?:\.git)?/?$', repository_url or '')
    if not match or not re.fullmatch(r'[\w./-]+', path) or '..' in path:
        return None
    if match.group(1) == 'github.com':
        return f'https://raw.githubusercontent.com/{match.group(2)}/HEAD/{path}'
    return f'https://gitlab.com/{match.group(2)}/-/raw/HEAD/{path}'


def fetch_repository_file(repository_url, path, problems):
    url = raw_file_url(repository_url, path)
    if not url:
        return None
    try:
        response = http.get(url, timeout=30)
        if response.status_code == 404:
            return None
        response.raise_for_status()
    except requests.RequestException as error:
        problems.append(f'could not fetch {path} from {repository_url}: {error}')
        return None
    return response.text


def fetch_repository_citation(repository_url, listed_path, problems):
    # The ecosyste.ms file list can lag behind, so always try the conventional path too.
    paths = [path for path in (listed_path, 'CITATION.cff') if path and path.lower().endswith(('.cff', '.bib'))]
    for path in dict.fromkeys(paths):
        text = fetch_repository_file(repository_url, path, problems)
        if text is None:
            continue
        citation = citation_from_bibtex(text) if path.lower().endswith('.bib') else citation_from_cff(text)
        if citation:
            return citation
        warn(f'ignoring malformed {path} in {repository_url}')
    return None


def dois_in_text(text):
    dois = []
    for match in re.finditer(r'\b10\.\d{4,9}/[A-Za-z0-9:/_;.\-]+|zenodo\.org/records?/(\d+)', text):
        if match.group(1):
            dois.append(f'10.5281/zenodo.{match.group(1)}')
            continue
        # Zenodo and JOSS badges put the DOI inside the image path.
        doi = re.sub(r'(/status)?\.(svg|png)$', '', match.group(0).rstrip('.,:;/_-'))
        # shields.io badges also end in a colour, as in DOI-10.5281/zenodo.1-blue.
        if re.search(r'shields\.io/badge/\S*$', text[max(0, match.start() - 100):match.start()]):
            doi = doi.rsplit('-', 1)[0]
        dois.append(doi.lower())
    return list(dict.fromkeys(dois))


def arxiv_review(text, place):
    ids = list(dict.fromkeys(re.findall(r'(?i)arxiv[.:/ ]+(?:org/(?:abs|pdf)/)?(\d{4}\.\d{4,5})', text)))
    if not ids:
        return []
    return [f"{place} mentions arXiv papers, check whether to cite them: {', '.join(f'arXiv:{item}' for item in ids)}"]


def zenodo_url(doi):
    match = re.fullmatch(r'10\.5281/zenodo\.(\d+)', doi)
    return f'{ZENODO_API}/{match.group(1)}' if match else None


def resolve_zenodo_badges(text, problems):
    """Badges of this form name a GitHub repository, and only the redirect gives the DOI."""
    dois = []
    for path in dict.fromkeys(re.findall(r'zenodo\.org/badge/latestdoi/[\w./-]+', text)):
        try:
            response = http.get(f"https://{path.rstrip('.')}", allow_redirects=False, timeout=30)
        except requests.RequestException as error:
            problems.append(f'could not resolve the Zenodo badge {path}: {error}')
            continue
        dois += dois_in_text(response.headers.get('location', ''))
    return dois


def fetch_zenodo_record(doi, problems):
    url = zenodo_url(doi)
    if not url:
        return None
    try:
        response = http.get(url, timeout=30)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        record = response.json()
        # A DOI can name an old release, but the concept record always leads to the newest.
        concept_url = f"{ZENODO_API}/{record['conceptrecid']}" if record.get('conceptrecid') else url
        if concept_url != url:
            response = http.get(concept_url, timeout=30)
            response.raise_for_status()
            record = response.json()
        return record
    except (requests.RequestException, ValueError) as error:
        problems.append(f'could not fetch {doi} from Zenodo: {error}')
        return None


def links_to_repository(record, repository_url):
    repository = normalize_url(repository_url)
    related = (record.get('metadata') or {}).get('related_identifiers') or []
    urls = [normalize_url(item.get('identifier', '')) for item in related]
    return any(url == repository or url.startswith(f'{repository}/') for url in urls)


def fetch_zenodo_citation(doi, record, problems):
    url = f"{ZENODO_API}/{record['conceptrecid']}" if record.get('conceptrecid') else zenodo_url(doi)
    try:
        response = http.get(url, headers={'Accept': 'application/x-bibtex'}, timeout=30)
        response.raise_for_status()
    except requests.RequestException as error:
        problems.append(f'could not fetch {doi} from Zenodo: {error}')
        return None
    citation = citation_from_bibtex(response.text)
    if not citation:
        warn(f'ignoring malformed BibTeX for {doi} from Zenodo')
        return None
    citation['source'] = 'Zenodo'
    citation['zenodo_version'] = (record.get('metadata') or {}).get('version')
    citation['dois'] = (citation['dois'] | {doi, normalize_doi(record.get('conceptdoi') or '')}) - {None}
    return citation


def citation_from_mentions(text, repository_url, place, problems):
    """Builds a citation from the DOIs and Zenodo links on a page, or returns None."""
    dois = dois_in_text(text)
    if not any(zenodo_url(doi) for doi in dois):
        dois = list(dict.fromkeys(dois + resolve_zenodo_badges(text, problems)))
    if not dois:
        return None
    records = {doi: record for doi in dois if (record := fetch_zenodo_record(doi, problems))}
    # Links to different releases of one project count as a single DOI.
    distinct = list(dict.fromkeys((records[doi].get('conceptdoi') or doi).lower() if doi in records else doi for doi in dois))
    # With several DOIs, only a person can tell which one identifies this project.
    review = [f"{place} lists several DOIs, check which to cite: {', '.join(distinct)}"] if len(distinct) > 1 else []
    linked = [doi for doi, record in records.items() if links_to_repository(record, repository_url)]
    for doi in linked or list(records):
        citation = fetch_zenodo_citation(doi, records[doi], problems)
        if citation:
            citation['found_in'] = place
            citation['dois'] = citation['dois'] | set(dois)
            citation['review'] = review
            return citation
    return {'source': place, 'dois': set(dois), 'doi': dois[0] if len(distinct) == 1 else None, 'review': review}


def fetch_freeform_citation(repository_url, listed_path, problems):
    if not listed_path or listed_path.lower().endswith(('.cff', '.bib')):
        return None
    text = fetch_repository_file(repository_url, listed_path, problems)
    if text is None:
        return None
    citation = (citation_from_bibtex(text)
                or citation_from_mentions(text, repository_url, listed_path, problems)
                or {'dois': set()})
    citation['source'] = listed_path
    citation['review'] = [f'{listed_path} is not in a standard format, check it'] + citation.get('review', [])
    return citation


def fetch_readme(repository_url, listed_path, problems):
    # ecosyste.ms sometimes lists a translation or a secondary README.
    if listed_path and re.fullmatch(r'(?i)README(\.(md|rst|txt))?', listed_path):
        paths = [listed_path]
    else:
        paths = ['README.md', 'README.rst'] + ([listed_path] if listed_path else [])
    for path in paths:
        text = fetch_repository_file(repository_url, path, problems)
        if text is not None:
            return text
    return None


@lru_cache(maxsize=None)
def warn_once(message):
    warn(message)


def find_docs_pages(repository_url, problems):
    """Returns the paths of the citing pages and of the index page in a project's docs."""
    match = re.match(r'https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$', repository_url)
    if not match:
        return [], []
    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        warn_once('set GITHUB_TOKEN to also check project docs')
        return [], []
    try:
        response = http.get(f'https://api.github.com/repos/{match.group(1)}/git/trees/HEAD', params={'recursive': 1},
                            headers={'Authorization': f'token {token}'}, timeout=30)
        response.raise_for_status()
        paths = [item.get('path', '') for item in response.json().get('tree', [])]
    except (requests.RequestException, ValueError) as error:
        problems.append(f'could not list the docs of {repository_url}: {error}')
        return [], []
    citing = [path for path in paths if re.fullmatch(
        r'(?i)docs?/(?:[^/]+/)*[^/]*(?<![a-z])cit(?:e|ing|ations?)(?![a-z])[^/]*\.(?:rst|md|txt)', path)]
    index = [path for path in paths if re.fullmatch(r'(?i)docs?/(?:source/)?index\.(?:rst|md|txt)', path)]
    return citing, index


def citation_from_docs(repository_url, problems):
    citing, index = find_docs_pages(repository_url, problems)
    pages = [fetch_repository_file(repository_url, path, problems) for path in citing + index]
    text = '\n'.join(page for page in pages if page)
    citation = citation_from_mentions(text, repository_url, 'docs', problems)
    review = arxiv_review(text, 'docs')
    if not citation and citing:
        links = ', '.join(f'{repository_url}/blob/HEAD/{path}' for path in citing)
        review = [f'docs may say how to cite this project: {links}'] + review
    if citation:
        citation['review'] = citation.get('review', []) + review
    elif review:
        citation = {'source': 'docs', 'dois': set(), 'doi': None, 'review': review}
    return citation


def find_citation(repository_url, files, problems):
    citation = fetch_repository_citation(repository_url, files.get('citation'), problems)
    if citation:
        return citation
    citation = fetch_freeform_citation(repository_url, files.get('citation'), problems)
    readme = fetch_readme(repository_url, files.get('readme'), problems) or ''
    if not citation:
        citation = citation_from_mentions(readme, repository_url, 'README', problems)
    if not citation:
        citation = citation_from_docs(repository_url, problems)
    review = arxiv_review(readme, 'README')
    if review and not citation:
        citation = {'source': 'README', 'dois': set(), 'doi': None}
    if citation:
        citation['review'] = citation.get('review', []) + review
    return citation


def lookup_packages(dependencies, problems):
    def key_of(purl):
        ecosystem, name = purl.removeprefix('pkg:').split('/', 1)
        return (ecosystem, normalize_name(ecosystem, unquote(name)).lower())

    wanted = sorted({package_url(dependency) for dependency in dependencies})
    packages = {}
    for start in range(0, len(wanted), BATCH_SIZE):
        batch = wanted[start:start + BATCH_SIZE]
        try:
            response = http.post(f'{PACKAGES_API}/packages/bulk_lookup', json={'purls': batch}, timeout=60)
            response.raise_for_status()
            results = response.json()
        except (requests.RequestException, ValueError) as error:
            problems.append(f'could not look up {len(batch)} packages on ecosyste.ms: {error}')
            continue
        for package in results:
            # Results come back unordered, and a qualified purl means a registry mirror.
            if package.get('purl') and '?' not in package['purl']:
                packages[key_of(package['purl'])] = package
    return {package_url(dependency): packages.get(key_of(package_url(dependency))) for dependency in dependencies}


def build_citations(dependencies, packages, problems):
    citations = []
    by_repository = {}
    for dependency in dependencies:
        name = dependency['name']
        package = packages.get(package_url(dependency))
        fallback_urls = {normalize_url(name)} if dependency['ecosystem'] == 'golang' else set()
        citation = {
            'package': name,
            'ecosystem': dependency['ecosystem'],
            'version': dependency['version'],
            'record': package,
            'repository': None,
            'type': 'software',
            'title': name,
            'authors': [],
            'year': None,
            'doi': None,
            'url': None,
            'journal': None,
            'source': None,
            'dois': set(),
            'urls': fallback_urls,
            'titles': {normalize_title(name)},
        }
        if package:
            repository = package.get('repository_url')
            citation['repository'] = repository
            citation['source'] = 'ecosyste.ms'
            citation['title'] = package.get('name') or name
            if repository:
                citation['urls'] = citation['urls'] | {normalize_url(repository)}
                files = ((package.get('repo_metadata') or {}).get('metadata') or {}).get('files') or {}
                if repository not in by_repository:
                    by_repository[repository] = find_citation(repository, files, problems)
                found = by_repository[repository]
                if found:
                    citation.update({key: found[key] for key in CITATION_FIELDS if key in found})
                    citation['dois'] = found['dois']
                    citation['urls'] = citation['urls'] | found.get('urls', set())
                    citation['titles'] = citation['titles'] | found.get('titles', set())
        citations.append(citation)
    return citations


def load_references(path):
    text = Path(path).read_text()
    if Path(path).suffix.lower() == '.bib':
        return [identifiers_of(bibtex_record(entry['fields'])) for entry in parse_bibtex(text)]
    cff = yaml.safe_load(text)
    if not isinstance(cff, dict):
        raise ValueError(f'{path} is not a valid CITATION.cff file')
    return [identifiers_of(reference) for reference in cff.get('references') or [] if isinstance(reference, dict)]


def audit(citations, references):
    for citation in citations:
        citation['matched_on'] = None
        for kind, label in (('dois', 'DOI'), ('urls', 'repository URL'), ('titles', 'title')):
            if any(citation[kind] & reference[kind] for reference in references):
                citation['matched_on'] = label
                break


# Writing citations

@lru_cache(maxsize=None)
def get_github_name(username, token):
    headers = {'Authorization': f'token {token}'}
    response = http.get(f'https://api.github.com/users/{username}', headers=headers, timeout=30)
    user_data = response.json() if response.ok else {}
    return user_data.get('name') or username


def get_authors_from_github(repo_path):
    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        return []
    headers = {'Authorization': f'token {token}'}
    try:
        response = http.get(f'https://api.github.com/repos/{repo_path}/contributors', headers=headers, timeout=30)
        contributors = response.json() if response.ok else []
        return [get_github_name(contributor['login'], token) for contributor in contributors
                if contributor.get('type') == 'User']
    except requests.RequestException as error:
        warn(f'could not list contributors of {repo_path}: {error}')
        return []


def best_guess_authors(citation):
    package = citation['record'] or {}
    match = re.match(r'https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$', citation['repository'] or '')
    names = get_authors_from_github(match.group(1)) if match else []
    if not names:
        names = [maintainer.get('name') or maintainer.get('login')
                 for maintainer in package.get('maintainers') or []]
    if not names:
        repository = package.get('repo_metadata') or {}
        names = [(repository.get('owner_record') or {}).get('name') or repository.get('owner')]
    names = [name for name in names if name] or [f"{citation['package']} contributors"]
    return [{'name': name} for name in names]


def release_year(citation):
    package = citation['record'] or {}
    published = None
    registry = REGISTRIES.get(citation['ecosystem'])
    if citation['version'] and registry:
        name = quote(citation['package'], safe='')
        url = f"{PACKAGES_API}/registries/{registry}/packages/{name}/versions/{quote(citation['version'], safe='')}"
        try:
            response = http.get(url, timeout=30)
            if response.ok:
                published = response.json().get('published_at')
        except (requests.RequestException, ValueError) as error:
            warn(f"could not look up the release of {citation['package']}: {error}")
    published = published or package.get('latest_release_published_at')
    return datetime.fromisoformat(published.replace('Z', '+00:00')).year if published else None


def fill_in_details(citation):
    if not citation['authors']:
        citation['authors'] = best_guess_authors(citation)
    if not citation['year']:
        citation['year'] = release_year(citation)


def unique_citations(citations):
    """A monorepo or a shared paper only needs citing once."""
    unique = {}
    for citation in citations:
        key = citation['doi'] or citation['repository'] or citation['package']
        unique.setdefault(key, citation)
    return list(unique.values())


def author_text(author):
    if 'family-names' not in author:
        return author.get('name') or author.get('alias') or author.get('given-names', '')
    parts = [author.get('given-names'), author.get('name-particle'), author['family-names']]
    return ' '.join(part for part in parts if part)


def bibtex_author(author):
    if 'family-names' not in author:
        return f'{{{clean(author_text(author))}}}'
    family = ' '.join(part for part in (author.get('name-particle'), author['family-names']) if part)
    given = author.get('given-names')
    return clean(f'{family}, {given}' if given else family)


def cited_version(citation):
    if citation['type'] != 'software':
        return None
    # A Zenodo record describes one release, which may not be the one in use.
    return citation.get('zenodo_version') or citation['version']


def review_note(citation):
    return '; '.join(citation.get('review') or []) or None


def bibtex_entry(citation, key):
    note = review_note(citation)
    comment = f'% NEEDS REVIEW: {note}\n' if note else ''
    fields = [
        ('author', ' and '.join(bibtex_author(author) for author in citation['authors'])),
        ('title', citation['title']),
        ('journal', citation['journal']),
        ('url', citation['url'] or citation['repository']),
        ('version', cited_version(citation)),
        ('date', citation['year']),
        ('doi', citation['doi']),
    ]
    lines = ''.join(
        f"  {name} = {{{value if name == 'author' else clean(value)}}},\n"
        for name, value in fields if value
    )
    return f"\n{comment}@{CFF_TO_BIB.get(citation['type'], 'misc')}{{{key},\n{lines}}}\n"


def write_bibtex_to_file(citations, path):
    entries = []
    used = set()
    for citation in citations:
        base = re.sub(r'[^a-z0-9]+', '-', citation['package'].lower()).strip('-')
        key, count = base, 1
        while key in used:
            count += 1
            key = f'{base}-{count}'
        used.add(key)
        entries.append(bibtex_entry(citation, key))
    Path(path).write_text(''.join(entries))


def cff_reference(citation):
    reference = {
        'type': citation['type'] if citation['type'] in CFF_TYPES else 'generic',
        'title': citation['title'],
        'authors': citation['authors'],
    }
    optional = {
        'year': citation['year'],
        'version': cited_version(citation),
        'doi': citation['doi'],
        'repository-code': citation['repository'],
        'url': citation['url'] if citation['url'] != citation['repository'] else None,
        'journal': citation['journal'],
        'notes': review_note(citation),
    }
    reference.update({key: value for key, value in optional.items() if value})
    return reference


def write_cff_to_file(citations, path):
    references = {'references': [cff_reference(citation) for citation in citations]}
    Path(path).write_text(yaml.safe_dump(references, sort_keys=False, allow_unicode=True))


def write_citations(citations, path):
    """Writes usable citations; returns the packages with too little data to cite."""
    known = [citation for citation in citations if citation['record']]
    unique = unique_citations(known)
    for citation in unique:
        fill_in_details(citation)
    if Path(path).suffix.lower() == '.bib':
        write_bibtex_to_file(unique, path)
    else:
        write_cff_to_file(unique, path)
    return unique, [citation['package'] for citation in citations if not citation['record']]


# Reporting

def describe_source(citation):
    if citation['source'] is None:
        return 'not found on ecosyste.ms'
    if citation['source'] == 'ecosyste.ms':
        return 'no citation file'
    if citation['source'] in ('README', 'docs'):
        if not citation['dois']:
            return f"flagged in {citation['source']}"
        return f"DOI in {citation['source']}: {', '.join(sorted(citation['dois']))}"
    if citation['source'] == 'Zenodo':
        return f"Zenodo DOI in {citation.get('found_in', 'README')}: {citation['doi']}"
    return f"has {citation['source']}"


def asked_to_be_cited(citation):
    return citation['source'] not in (None, 'ecosyste.ms')


def label(citation):
    return f"{citation['package']} {citation['version']}" if citation['version'] else citation['package']


def print_review_list(citations, file=None):
    flagged = [citation for citation in citations if citation.get('review')]
    if flagged:
        print('\nNeeds review:', file=file)
        for citation in sorted(flagged, key=lambda item: item['package'].lower()):
            print(f'  {label(citation)}: {review_note(citation)}', file=file)


def print_report(citations, cited_in, problems):
    missing = [citation for citation in citations if not citation['matched_on']]
    cited = [citation for citation in citations if citation['matched_on']]
    print(f'Checked {len(citations)} dependencies against {cited_in}: {len(cited)} cited, {len(missing)} missing.')
    if missing:
        print('\nMissing:')
        for citation in sorted(missing, key=lambda item: item['package'].lower()):
            print(f'  {label(citation)} ({describe_source(citation)})')
    if cited:
        print('\nCited:')
        for citation in sorted(cited, key=lambda item: item['package'].lower()):
            print(f"  {label(citation)} (matched on {citation['matched_on']})")
    print_review_list(missing)
    if problems:
        print('\nThe check is incomplete:')
        for problem in problems:
            print(f'  {problem}')


def print_json_report(citations, cited_in, problems):
    print(json.dumps({
        'cited_in': str(cited_in),
        'dependencies': [{
            'package': citation['package'],
            'ecosystem': citation['ecosystem'],
            'version': citation['version'],
            'cited': bool(citation['matched_on']),
            'matched_on': citation['matched_on'],
            'citation_source': citation['source'],
            'dois': sorted(citation['dois']),
            'needs_review': bool(citation.get('review')),
            'review': citation.get('review') or [],
        } for citation in sorted(citations, key=lambda item: item['package'].lower())],
        'problems': problems,
    }, indent=2))


def parse_args(argv):
    parser = argparse.ArgumentParser(description='Cite all of the software dependencies of a project.')
    parser.add_argument('project', nargs='?', default='.', help='project directory (default: current directory)')
    parser.add_argument('--check', action='store_true', help='report which dependencies are not cited, instead of generating citations')
    parser.add_argument('--cited-in', help='.cff or .bib file to check against (default: CITATION.cff in the project)')
    parser.add_argument('--output', help='file to write citations to; .bib for BibTeX, anything else for CFF references (default: output.bib, or nothing with --check)')
    parser.add_argument('--sbom', help='read dependencies from a CycloneDX or SPDX JSON SBOM instead of the project files')
    parser.add_argument('--json', action='store_true', help='print the --check report as JSON')
    parser.add_argument('--asked-to-be-cited', action='store_true', help='only include dependencies that have asked to be cited: a citation file, or a DOI, Zenodo link or arXiv paper in their README or docs')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    project = Path(args.project)
    problems = []

    try:
        cited_in = None
        if args.check:
            cited_in = Path(args.cited_in) if args.cited_in else project / 'CITATION.cff'
            references = load_references(cited_in)
        dependencies = find_dependencies(project, args.sbom, problems)
    except (OSError, ValueError, KeyError, yaml.YAMLError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2

    if not dependencies:
        print(f'error: no dependencies found in {project}', file=sys.stderr)
        for problem in problems:
            print(f'  {problem}', file=sys.stderr)
        return 2

    packages = lookup_packages(dependencies, problems)
    citations = build_citations(dependencies, packages, problems)

    if args.asked_to_be_cited:
        total = len(citations)
        citations = [citation for citation in citations if asked_to_be_cited(citation)]
        print(f'{len(citations)} of {total} dependencies have asked to be cited.', file=sys.stderr)

    if args.check:
        audit(citations, references)
        missing = [citation for citation in citations if not citation['matched_on']]
        if args.json:
            print_json_report(citations, cited_in, problems)
        else:
            print_report(citations, cited_in, problems)
        if args.output and missing:
            written, skipped = write_citations(missing, args.output)
            print(f'\nWrote {len(written)} citations to {args.output}.', file=sys.stderr)
            if skipped:
                warn(f"too little data to cite: {', '.join(skipped)}")
        if missing:
            return 1
        return 2 if problems else 0

    if not citations:
        for problem in problems:
            warn(problem)
        return 2 if problems else 0

    output = args.output or 'output.bib'
    written, skipped = write_citations(citations, output)
    today = datetime.today().strftime('%Y-%m-%d')
    for citation in written:
        authors = ', '.join(author_text(author) for author in citation['authors'])
        print(f"{authors} ({citation['year'] or 'n.d.'}). {citation['title']}. Retrieved from {citation['url'] or citation['repository']} on {today}.")
    if skipped:
        warn(f"too little data to cite: {', '.join(skipped)}")
    for problem in problems:
        warn(problem)
    print_review_list(written, sys.stderr)
    print(f'\nWrote {len(written)} citations to {output}.', file=sys.stderr)
    return 2 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
