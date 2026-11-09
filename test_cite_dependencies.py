import json

import pytest
import yaml

import cite_dependencies as cite


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text='', headers=None):
        self.status_code = status_code
        self.payload = payload
        self.text = text
        self.headers = headers or {}
        self.ok = status_code < 400
        self.is_redirect = status_code in (301, 302)

    def json(self):
        return self.payload

    def raise_for_status(self):
        if not self.ok:
            raise cite.requests.HTTPError(f'{self.status_code} error')


class FakeHttp:
    """Serves GET routes by URL and answers bulk lookups and resolver jobs."""

    def __init__(self, packages=None, files=None, jobs=None, zenodo=None, redirects=None, trees=None):
        self.packages = packages or []
        self.files = files or {}
        self.jobs = jobs or {}
        self.zenodo = zenodo or {}
        self.redirects = redirects or {}
        self.trees = trees or {}
        self.job_params = []

    def post(self, url, params=None, json=None, **kwargs):
        if url.endswith('/packages/bulk_lookup'):
            return FakeResponse(payload=self.packages)
        self.job_params.append(params)
        return FakeResponse(301, headers={'location': f"https://jobs.test/{params['package_name']}"})

    def get(self, url, **kwargs):
        if url.startswith('https://jobs.test/'):
            return FakeResponse(payload=self.jobs[url.rsplit('/', 1)[1]])
        if url in self.files:
            return FakeResponse(text=self.files[url])
        if url in self.redirects:
            return FakeResponse(302, headers={'location': self.redirects[url]})
        if url in self.trees:
            return FakeResponse(payload={'tree': [{'path': path} for path in self.trees[url]]})
        if url in self.zenodo:
            record, bibtex = self.zenodo[url]
            if kwargs.get('headers', {}).get('Accept') == 'application/x-bibtex':
                return FakeResponse(text=bibtex)
            return FakeResponse(payload=record)
        return FakeResponse(404)


def package(name, repository, citation_file=None, ecosystem='pypi'):
    return {
        'purl': f'pkg:{ecosystem}/{name}',
        'name': name,
        'repository_url': repository,
        'maintainers': [{'login': 'maintainer', 'name': 'Some Maintainer'}],
        'latest_release_published_at': '2024-05-01T00:00:00.000Z',
        'repo_metadata': {'metadata': {'files': {'citation': citation_file}}},
    }


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(cite.time, 'sleep', lambda seconds: None)


@pytest.fixture(autouse=True)
def no_github_token(monkeypatch):
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    cite.warn_once.cache_clear()


@pytest.mark.parametrize('line,name,version', [
    ('numpy==1.21.0', 'numpy', '1.21.0'),
    ('Ruamel.Yaml>=0.17', 'ruamel-yaml', None),
    ('requests[socks]==2.31.0 ; python_version > "3"', 'requests', '2.31.0'),
    ('flask==3.0.0 --hash=sha256:abc \\', 'flask', '3.0.0'),
    ('scipy', 'scipy', None),
    ('foo~=1.0', 'foo', None),
])
def test_parse_requirement(line, name, version):
    assert cite.parse_requirement(line) == {'ecosystem': 'pypi', 'name': name, 'version': version}


@pytest.mark.parametrize('line', ['', '# comment', '-r other.txt', '--hash=sha256:abc'])
def test_parse_requirement_ignores_non_requirements(line):
    assert cite.parse_requirement(line) is None


def test_uv_lock_skips_the_project_itself(tmp_path):
    path = tmp_path / 'uv.lock'
    path.write_text('''
[[package]]
name = "mine"
version = "0.1.0"
source = { editable = "." }

[[package]]
name = "Typing_Extensions"
version = "4.0.0"
source = { registry = "https://pypi.org/simple" }
''')
    assert cite.read_python_lock(path) == [cite.make_dependency('pypi', 'typing-extensions', '4.0.0')]


def test_package_lock_reads_scoped_and_nested_packages(tmp_path):
    path = tmp_path / 'package-lock.json'
    path.write_text(json.dumps({'packages': {
        '': {'name': 'mine'},
        'node_modules/@babel/core': {'version': '7.0.0'},
        'node_modules/a/node_modules/b': {'version': '2.0.0'},
        'node_modules/local': {'link': True},
    }}))
    names = {(item['name'], item['version']) for item in cite.read_package_lock(path)}
    assert names == {('@babel/core', '7.0.0'), ('b', '2.0.0')}


def test_cargo_lock_skips_workspace_members(tmp_path):
    path = tmp_path / 'Cargo.lock'
    path.write_text('''
[[package]]
name = "mine"
version = "0.1.0"

[[package]]
name = "serde"
version = "1.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
''')
    assert cite.read_cargo_lock(path) == [cite.make_dependency('cargo', 'serde', '1.0.0')]


def test_gemfile_lock_reads_only_top_level_gem_specs(tmp_path):
    path = tmp_path / 'Gemfile.lock'
    path.write_text('''PATH
  remote: .
  specs:
    mine (0.1.0)

GEM
  remote: https://rubygems.org/
  specs:
    rails (7.0.0)
      actionpack (= 7.0.0)

DEPENDENCIES
  rails
''')
    assert cite.read_gemfile_lock(path) == [cite.make_dependency('gem', 'rails', '7.0.0')]


def test_go_mod_reads_block_and_single_line_requires(tmp_path):
    path = tmp_path / 'go.mod'
    path.write_text('''module example.com/mine

go 1.22

require github.com/a/b v1.2.3

require (
    github.com/c/d v2.0.0+incompatible
    github.com/e/f v0.1.0 // indirect
)

replace github.com/a/b v1.2.3 => ../b
''')
    assert [(item['name'], item['version']) for item in cite.read_go_mod(path)] == [
        ('github.com/a/b', '1.2.3'), ('github.com/c/d', '2.0.0'), ('github.com/e/f', '0.1.0'),
    ]


def test_sbom_reads_cyclonedx_and_spdx(tmp_path):
    cyclonedx = tmp_path / 'cdx.json'
    cyclonedx.write_text(json.dumps({'components': [{'purl': 'pkg:npm/%40babel/core@7.0.0'}]}))
    assert cite.read_sbom(cyclonedx) == [cite.make_dependency('npm', '@babel/core', '7.0.0')]

    spdx = tmp_path / 'spdx.json'
    spdx.write_text(json.dumps({'packages': [
        {'externalRefs': [{'referenceType': 'purl', 'referenceLocator': 'pkg:github/me/mine@main'}]},
        {'externalRefs': [{'referenceType': 'purl', 'referenceLocator': 'pkg:pypi/six@1.16.0'}]},
    ]}))
    assert cite.read_sbom(spdx) == [cite.make_dependency('pypi', 'six', '1.16.0')]


def test_lockfile_takes_priority_over_manifest(tmp_path, monkeypatch):
    (tmp_path / 'requirements.txt').write_text('notused==1.0\n')
    (tmp_path / 'uv.lock').write_text('[[package]]\nname = "locked"\nversion = "2.0"\n')
    fake = FakeHttp()
    monkeypatch.setattr(cite, 'http', fake)
    found = cite.find_dependencies(tmp_path, None, [])
    assert [item['name'] for item in found] == ['locked']
    assert fake.job_params == []


def test_resolver_adds_transitive_and_keeps_pins(tmp_path, monkeypatch, no_sleep):
    (tmp_path / 'requirements.txt').write_text('requests==2.31.0\nurllib3==1.26.0\n')
    fake = FakeHttp(jobs={
        'requests': {'status': 'complete', 'results': {'urllib3': '2.0.0', 'idna': '3.0'}},
        'urllib3': {'status': 'complete', 'results': {}},
    })
    monkeypatch.setattr(cite, 'http', fake)
    problems = []
    found = {item['name']: item['version'] for item in cite.find_dependencies(tmp_path, None, problems)}
    assert found == {'requests': '2.31.0', 'urllib3': '1.26.0', 'idna': '3.0'}
    assert problems == []
    assert all(params['registry'] == 'pypi.org' for params in fake.job_params)


def test_ranges_are_not_sent_to_the_resolver(tmp_path, monkeypatch, no_sleep):
    (tmp_path / 'requirements.txt').write_text('requests>=2.0\n')
    fake = FakeHttp(jobs={'requests': {'status': 'complete', 'results': {}}})
    monkeypatch.setattr(cite, 'http', fake)
    cite.find_dependencies(tmp_path, None, [])
    assert 'version' not in fake.job_params[0]


def test_resolver_failure_is_reported_not_swallowed(tmp_path, monkeypatch, no_sleep):
    (tmp_path / 'requirements.txt').write_text('nothere\n')
    fake = FakeHttp(jobs={'nothere': {'status': 'error', 'results': {'error': 'package not found'}}})
    monkeypatch.setattr(cite, 'http', fake)
    problems = []
    found = cite.find_dependencies(tmp_path, None, problems)
    assert [item['name'] for item in found] == ['nothere']
    assert 'package not found' in problems[0]


def test_bulk_lookup_handles_unordered_results_and_mirrors(monkeypatch):
    mirror = package('rails', 'https://mirror.test/rails', ecosystem='gem')
    mirror['purl'] = 'pkg:gem/rails?repository_url=https://gem.coop'
    fake = FakeHttp(packages=[
        package('Flask', 'https://github.com/pallets/flask'),
        package('rails', 'https://github.com/rails/rails', ecosystem='gem'),
        mirror,
    ])
    monkeypatch.setattr(cite, 'http', fake)
    dependencies = [cite.make_dependency('gem', 'rails'), cite.make_dependency('pypi', 'flask'), cite.make_dependency('pypi', 'absent')]
    found = cite.lookup_packages(dependencies, [])
    assert found['pkg:gem/rails']['repository_url'] == 'https://github.com/rails/rails'
    assert found['pkg:pypi/flask']['name'] == 'Flask'
    assert found['pkg:pypi/absent'] is None


def test_cff_prefers_preferred_citation_but_keeps_both_identifiers():
    citation = cite.citation_from_cff('''
cff-version: 1.2.0
title: mytool
doi: 10.5281/zenodo.1
repository-code: https://github.com/a/mytool
authors:
  - family-names: Root
preferred-citation:
  type: article
  title: "My Tool: A Paper"
  doi: https://doi.org/10.1000/PAPER
  journal: JOSS
  year: 2021
  authors:
    - given-names: Ada
      family-names: Lovelace
''')
    assert citation['type'] == 'article'
    assert citation['title'] == 'My Tool: A Paper'
    assert citation['authors'] == [{'given-names': 'Ada', 'family-names': 'Lovelace'}]
    assert citation['year'] == 2021
    assert citation['dois'] == {'10.1000/paper', '10.5281/zenodo.1'}
    assert 'github.com/a/mytool' in citation['urls']


def test_malformed_cff_is_rejected():
    assert cite.citation_from_cff('title: [unclosed') is None
    assert cite.citation_from_cff('just a string') is None


def test_cff_loader_refuses_python_tags():
    assert cite.citation_from_cff('title: !!python/object/apply:os.system ["true"]') is None


def test_bibtex_parser_handles_nesting_and_quotes():
    entries = cite.parse_bibtex('''
@comment{ignored}
@article{key,
  title = {A {Nested} Title},
  author = "Harris, Charles R. and Ada Lovelace and {NumPy Team}",
  year = 2020,
  doi = {10.1038/s41586-020-2649-2},
}
@misc{other, title={Second}, howpublished={\\url{https://github.com/a/b}}}
''')
    assert [entry['kind'] for entry in entries] == ['article', 'misc']
    assert entries[0]['fields']['year'] == '2020'
    assert cite.bibtex_authors(entries[0]['fields']['author']) == [
        {'family-names': 'Harris', 'given-names': 'Charles R.'},
        {'family-names': 'Lovelace', 'given-names': 'Ada'},
        {'name': 'NumPy Team'},
    ]
    citation = cite.citation_from_bibtex(entries and '@article{k, title={A {Nested} Title}, year=2020}')
    assert citation['title'] == 'A Nested Title'
    assert cite.identifiers_of(cite.bibtex_record(entries[1]['fields']))['urls'] == {'github.com/a/b'}


@pytest.mark.parametrize('url', [
    'https://github.com/Foo/Bar',
    'http://www.github.com/foo/bar/',
    'git+https://github.com/foo/bar.git',
    'git@github.com:foo/bar.git',
    'https://github.com/foo/bar/tree/v1.0',
])
def test_normalize_url(url):
    assert cite.normalize_url(url) == 'github.com/foo/bar'


@pytest.mark.parametrize('path', ['../secret', 'a b.cff', 'x?y=1.cff'])
def test_raw_file_url_rejects_unsafe_paths(path):
    assert cite.raw_file_url('https://github.com/a/b', path) is None


def test_raw_file_url_ignores_unsupported_hosts():
    assert cite.raw_file_url('https://example.com/a/b', 'CITATION.cff') is None


def citation_for(name, **overrides):
    citation = {'dois': set(), 'urls': set(), 'titles': {cite.normalize_title(name)}, 'package': name}
    citation.update(overrides)
    return citation


@pytest.mark.parametrize('citation,reference,matched_on', [
    (citation_for('x', dois={'10.1/a'}), {'dois': {'10.1/a'}, 'urls': set(), 'titles': set()}, 'DOI'),
    (citation_for('x', urls={'github.com/a/b'}), {'dois': set(), 'urls': {'github.com/a/b'}, 'titles': set()}, 'repository URL'),
    (citation_for('Requests'), cite.identifiers_of({'title': 'Requests: HTTP for Humans'}), 'title'),
    (citation_for('x'), {'dois': set(), 'urls': set(), 'titles': {'y'}}, None),
])
def test_audit_matching(citation, reference, matched_on):
    cite.audit([citation], [reference])
    assert citation['matched_on'] == matched_on


def full_citation(**overrides):
    citation = {
        'package': 'tool', 'ecosystem': 'pypi', 'version': '1.0', 'record': {}, 'repository': 'https://github.com/a/tool',
        'type': 'software', 'title': 'Tool', 'authors': [{'given-names': 'Ada', 'family-names': 'Lovelace'}, {'name': 'The Team'}],
        'year': 2024, 'doi': None, 'url': None, 'journal': None, 'source': 'ecosyste.ms',
    }
    citation.update(overrides)
    return citation


def test_bibtex_output_round_trips():
    entry = cite.bibtex_entry(full_citation(title='Bad } Title'), 'tool')
    assert entry.startswith('\n@software{tool,')
    assert 'author = {Lovelace, Ada and {The Team}},' in entry
    parsed = cite.parse_bibtex(entry)[0]['fields']
    assert parsed['title'] == 'Bad Title'
    assert parsed['version'] == '1.0'


def test_cff_output_has_required_keys(tmp_path):
    path = tmp_path / 'refs.cff'
    cite.write_cff_to_file([full_citation()], path)
    reference = yaml.safe_load(path.read_text())['references'][0]
    assert reference['type'] == 'software'
    assert reference['authors'][0]['family-names'] == 'Lovelace'
    assert reference['repository-code'] == 'https://github.com/a/tool'


def test_monorepo_packages_are_written_once():
    first = full_citation(package='@babel/core')
    second = full_citation(package='@babel/parser')
    other = full_citation(package='other', repository='https://github.com/b/other')
    assert [item['package'] for item in cite.unique_citations([first, second, other])] == ['@babel/core', 'other']


def project(tmp_path, references='references: []'):
    (tmp_path / 'requirements.txt').write_text('cited==1.0\nnotcited==2.0\n')
    (tmp_path / 'CITATION.cff').write_text(f'cff-version: 1.2.0\ntitle: mine\n{references}\n')
    return tmp_path


def audit_http():
    return FakeHttp(
        packages=[
            package('cited', 'https://github.com/a/cited', 'CITATION.cff'),
            package('notcited', 'https://github.com/b/notcited'),
        ],
        files={'https://raw.githubusercontent.com/a/cited/HEAD/CITATION.cff': 'title: Cited Tool\ndoi: 10.1000/cited\nauthors:\n  - name: Org\n'},
        jobs={name: {'status': 'complete', 'results': {}} for name in ('cited', 'notcited')},
    )


def test_check_exits_1_and_lists_missing(tmp_path, monkeypatch, no_sleep, capsys):
    references = 'references:\n  - type: software\n    title: anything\n    doi: 10.1000/CITED\n    authors:\n      - name: Org'
    monkeypatch.setattr(cite, 'http', audit_http())
    assert cite.main([str(project(tmp_path, references)), '--check']) == 1
    out = capsys.readouterr().out
    assert '1 cited, 1 missing' in out
    assert 'notcited 2.0 (no citation file)' in out
    assert 'cited 1.0 (matched on DOI)' in out


def test_check_exits_0_when_everything_is_cited(tmp_path, monkeypatch, no_sleep):
    references = 'references:\n  - {type: software, title: Cited Tool, authors: [{name: a}]}\n  - {type: software, title: notcited, authors: [{name: a}]}'
    monkeypatch.setattr(cite, 'http', audit_http())
    assert cite.main([str(project(tmp_path, references)), '--check']) == 0


def test_check_exits_2_when_lookups_fail_even_if_all_cited(tmp_path, monkeypatch, no_sleep, capsys):
    references = 'references:\n  - {type: software, title: cited, authors: [{name: a}]}\n  - {type: software, title: notcited, authors: [{name: a}]}'
    fake = audit_http()
    fake.jobs['cited'] = {'status': 'error', 'results': {'error': 'boom'}}
    fake.jobs['notcited'] = {'status': 'complete', 'results': {}}
    monkeypatch.setattr(cite, 'http', fake)
    assert cite.main([str(project(tmp_path, references)), '--check']) == 2
    assert 'incomplete' in capsys.readouterr().out


def test_check_requires_a_citation_file(tmp_path, capsys):
    (tmp_path / 'requirements.txt').write_text('a==1\n')
    assert cite.main([str(tmp_path), '--check']) == 2
    assert 'CITATION.cff' in capsys.readouterr().err


def test_check_with_no_dependencies_does_not_pass(tmp_path):
    (tmp_path / 'CITATION.cff').write_text('title: mine\n')
    assert cite.main([str(tmp_path), '--check']) == 2


def test_check_can_use_a_bib_file(tmp_path, monkeypatch, no_sleep, capsys):
    project(tmp_path)
    bib = tmp_path / 'paper.bib'
    bib.write_text('@misc{c, title={Whatever}, url={https://github.com/a/cited}}\n')
    monkeypatch.setattr(cite, 'http', audit_http())
    assert cite.main([str(tmp_path), '--check', '--cited-in', str(bib), '--json']) == 1
    report = json.loads(capsys.readouterr().out)
    by_name = {item['package']: item for item in report['dependencies']}
    assert by_name['cited']['matched_on'] == 'repository URL'
    assert by_name['notcited']['cited'] is False


def test_check_writes_only_missing_citations(tmp_path, monkeypatch, no_sleep):
    references = 'references:\n  - {type: software, title: Cited Tool, authors: [{name: a}]}'
    monkeypatch.setattr(cite, 'http', audit_http())
    output = tmp_path / 'missing.cff'
    cite.main([str(project(tmp_path, references)), '--check', '--output', str(output)])
    written = yaml.safe_load(output.read_text())['references']
    assert [item['title'] for item in written] == ['notcited']
    assert written[0]['authors'] == [{'name': 'Some Maintainer'}]
    assert written[0]['year'] == 2024


@pytest.mark.parametrize('text,dois', [
    ('[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.123.svg)](https://doi.org/10.5281/zenodo.123)', ['10.5281/zenodo.123']),
    ('[![JOSS](https://joss.theoj.org/papers/10.21105/joss.01234/status.svg)](https://doi.org/10.21105/joss.01234)', ['10.21105/joss.01234']),
    ('See doi:10.1000/ABC. Also (10.1000/def), and https://doi.org/10.1000/ghi#x', ['10.1000/abc', '10.1000/def', '10.1000/ghi']),
    ('Serve on 10.0.0.1/24 from version 10.2/3', []),
    ('.. |DOI| image:: https://img.shields.io/badge/DOI-10.5281/zenodo.595120-blue.svg\n   :target: https://doi.org/10.5281/zenodo.595120', ['10.5281/zenodo.595120']),
    ('[![DOI](https://img.shields.io/badge/DOI-10.5281/zenodo.7-green)](https://example.com)', ['10.5281/zenodo.7']),
    ('Not a badge: https://example.com/10.1000/some-name.svg', ['10.1000/some-name']),
    ('https://zenodo.org/records/123 and https://zenodo.org/record/456/files/x.zip', ['10.5281/zenodo.123', '10.5281/zenodo.456']),
    ('https://zenodo.org/doi/10.5281/zenodo.789', ['10.5281/zenodo.789']),
])
def test_dois_in_text(text, dois):
    assert cite.dois_in_text(text) == dois


def asking_http():
    return FakeHttp(
        packages=[
            package('hasfile', 'https://github.com/a/hasfile', 'CITATION.cff'),
            package('onedoi', 'https://github.com/b/onedoi'),
            package('twodois', 'https://github.com/c/twodois'),
            package('nothing', 'https://github.com/d/nothing'),
        ],
        files={
            'https://raw.githubusercontent.com/a/hasfile/HEAD/CITATION.cff': 'title: Has File\nauthors:\n  - name: Org\n',
            'https://raw.githubusercontent.com/b/onedoi/HEAD/README.md': 'https://doi.org/10.5281/zenodo.1',
            'https://raw.githubusercontent.com/c/twodois/HEAD/README.md': '10.1000/a and 10.1000/b',
            'https://raw.githubusercontent.com/d/nothing/HEAD/README.md': 'No identifiers here.',
        },
        jobs={name: {'status': 'complete', 'results': {}} for name in ('hasfile', 'onedoi', 'twodois', 'nothing', 'absent')},
    )


def asking_project(tmp_path):
    (tmp_path / 'requirements.txt').write_text('hasfile==1\nonedoi==1\ntwodois==1\nnothing==1\nabsent==1\n')
    (tmp_path / 'CITATION.cff').write_text('title: mine\nreferences:\n  - {type: generic, title: x, doi: 10.5281/zenodo.1, authors: [{name: a}]}\n')
    return tmp_path


def test_readme_doi_is_found_at_the_listed_path(monkeypatch):
    record = package('tool', 'https://github.com/a/tool')
    record['repo_metadata']['metadata']['files']['readme'] = 'docs/README.rst'
    fake = FakeHttp(files={'https://raw.githubusercontent.com/a/tool/HEAD/docs/README.rst': '10.1000/tool'})
    monkeypatch.setattr(cite, 'http', fake)
    dependency = cite.make_dependency('pypi', 'tool')
    citation = cite.build_citations([dependency], {cite.package_url(dependency): record}, [])[0]
    assert (citation['source'], citation['doi']) == ('README', '10.1000/tool')


def test_asked_to_be_cited_filters_the_check_report(tmp_path, monkeypatch, no_sleep, capsys):
    monkeypatch.setattr(cite, 'http', asking_http())
    assert cite.main([str(asking_project(tmp_path)), '--check', '--asked-to-be-cited', '--json']) == 1
    report = {item['package']: item for item in json.loads(capsys.readouterr().out)['dependencies']}
    assert set(report) == {'hasfile', 'onedoi', 'twodois'}
    assert report['onedoi']['cited'] is True
    assert report['twodois']['dois'] == ['10.1000/a', '10.1000/b']


def test_readme_doi_counts_as_cited_without_the_flag(tmp_path, monkeypatch, no_sleep, capsys):
    monkeypatch.setattr(cite, 'http', asking_http())
    cite.main([str(asking_project(tmp_path)), '--check', '--json'])
    report = {item['package']: item for item in json.loads(capsys.readouterr().out)['dependencies']}
    assert report['onedoi']['matched_on'] == 'DOI'
    assert report['nothing']['cited'] is False


def test_asked_to_be_cited_filters_generated_citations(tmp_path, monkeypatch, no_sleep):
    monkeypatch.setattr(cite, 'http', asking_http())
    output = tmp_path / 'out.bib'
    assert cite.main([str(asking_project(tmp_path)), '--asked-to-be-cited', '--output', str(output)]) == 0
    text = output.read_text()
    assert 'title = {Has File}' in text
    assert 'doi = {10.5281/zenodo.1}' in text
    assert 'title = {twodois}' in text
    assert 'doi = {10.1000/a}' not in text
    assert '% NEEDS REVIEW: README lists several DOIs, check which to cite: 10.1000/a, 10.1000/b\n@software{twodois,' in text
    assert 'Needs review' not in text
    assert text.count('NEEDS REVIEW') == 1
    assert len(cite.parse_bibtex(text)) == 3
    assert 'nothing' not in text and 'absent' not in text


def test_several_dois_are_flagged_in_the_report(tmp_path, monkeypatch, no_sleep, capsys):
    monkeypatch.setattr(cite, 'http', asking_http())
    cite.main([str(asking_project(tmp_path)), '--check', '--asked-to-be-cited'])
    out = capsys.readouterr().out
    assert 'Needs review:\n  twodois 1: README lists several DOIs, check which to cite: 10.1000/a, 10.1000/b' in out


def test_several_dois_are_flagged_in_json(tmp_path, monkeypatch, no_sleep, capsys):
    monkeypatch.setattr(cite, 'http', asking_http())
    cite.main([str(asking_project(tmp_path)), '--check', '--json'])
    report = {item['package']: item for item in json.loads(capsys.readouterr().out)['dependencies']}
    assert report['twodois']['needs_review'] is True
    assert report['nothing']['needs_review'] is False


def test_several_dois_are_flagged_in_generated_cff(tmp_path, monkeypatch, no_sleep):
    monkeypatch.setattr(cite, 'http', asking_http())
    output = tmp_path / 'out.cff'
    cite.main([str(asking_project(tmp_path)), '--asked-to-be-cited', '--output', str(output)])
    references = {item['title']: item for item in yaml.safe_load(output.read_text())['references']}
    assert references['twodois']['notes'] == 'README lists several DOIs, check which to cite: 10.1000/a, 10.1000/b'
    assert 'notes' not in references['Has File']


def test_asked_to_be_cited_leaves_output_alone_when_nothing_qualifies(tmp_path, monkeypatch, no_sleep):
    (tmp_path / 'requirements.txt').write_text('nothing==1\n')
    output = tmp_path / 'out.bib'
    output.write_text('keep me')
    monkeypatch.setattr(cite, 'http', asking_http())
    assert cite.main([str(tmp_path), '--asked-to-be-cited', '--output', str(output)]) == 0
    assert output.read_text() == 'keep me'


def test_failed_readme_fetch_marks_the_run_incomplete(tmp_path, monkeypatch, no_sleep, capsys):
    (tmp_path / 'requirements.txt').write_text('nothing==1\n')
    (tmp_path / 'CITATION.cff').write_text('title: mine\n')
    fake = asking_http()
    original = fake.get
    monkeypatch.setattr(fake, 'get', lambda url, **kwargs: FakeResponse(500) if url.endswith('README.md') else original(url, **kwargs))
    monkeypatch.setattr(cite, 'http', fake)
    assert cite.main([str(tmp_path), '--check', '--asked-to-be-cited']) == 2
    assert 'could not fetch README.md' in capsys.readouterr().out


ZENODO_BIBTEX = '''@software{smith_2024_20,
  author       = {Ann Smith and
                  someone},
  title        = {a/tool: v0.5},
  month        = jan,
  year         = 2024,
  publisher    = {Zenodo},
  version      = {v0.5},
  doi          = {10.5281/zenodo.20},
  url          = {https://doi.org/10.5281/zenodo.20},
}
'''


def zenodo_record(kind='software', related='https://github.com/a/tool/tree/v0.5'):
    return {
        'conceptdoi': '10.5281/zenodo.10',
        'metadata': {
            'version': 'v0.5',
            'resource_type': {'type': kind},
            'related_identifiers': [{'identifier': related}] if related else [],
        },
    }


def zenodo_citation(monkeypatch, readme, zenodo):
    fake = FakeHttp(
        files={'https://raw.githubusercontent.com/a/tool/HEAD/README.md': readme},
        zenodo={f'https://zenodo.org/api/records/{key}': (value, ZENODO_BIBTEX) for key, value in zenodo.items()},
    )
    monkeypatch.setattr(cite, 'http', fake)
    dependency = cite.make_dependency('pypi', 'tool', '1.0')
    problems = []
    packages = {cite.package_url(dependency): package('tool', 'https://github.com/a/tool')}
    return cite.build_citations([dependency], packages, problems)[0], problems


def test_zenodo_doi_uses_the_record_bibtex(monkeypatch):
    citation, problems = zenodo_citation(monkeypatch, 'https://doi.org/10.5281/zenodo.10', {'10': zenodo_record()})
    assert problems == []
    assert citation['source'] == 'Zenodo'
    assert citation['title'] == 'a/tool: v0.5'
    assert citation['authors'] == [{'family-names': 'Smith', 'given-names': 'Ann'}, {'name': 'someone'}]
    assert citation['year'] == 2024
    assert citation['doi'] == '10.5281/zenodo.20'
    assert citation['dois'] == {'10.5281/zenodo.10', '10.5281/zenodo.20'}
    assert cite.asked_to_be_cited(citation)
    assert 'version = {v0.5}' in cite.bibtex_entry(citation, 'tool')
    assert cite.cff_reference(citation)['version'] == 'v0.5'


def test_zenodo_record_linked_to_the_repository_wins_among_several_dois(monkeypatch):
    citation, _ = zenodo_citation(monkeypatch, '10.1000/paper and 10.5281/zenodo.10', {'10': zenodo_record()})
    assert citation['source'] == 'Zenodo'
    assert citation['review'] == ['README lists several DOIs, check which to cite: 10.1000/paper, 10.5281/zenodo.10']
    assert '10.1000/paper' in citation['dois']


def test_unlinked_zenodo_record_is_used_and_flagged_among_several_dois(monkeypatch):
    citation, _ = zenodo_citation(monkeypatch, '10.1000/paper and 10.5281/zenodo.10', {'10': zenodo_record(related=None)})
    assert citation['source'] == 'Zenodo'
    assert citation['title'] == 'a/tool: v0.5'
    assert citation['review'] == ['README lists several DOIs, check which to cite: 10.1000/paper, 10.5281/zenodo.10']


def test_linked_record_is_preferred_among_several_zenodo_records(monkeypatch):
    other = zenodo_record(related='https://github.com/a/toolbox')
    other['conceptdoi'] = '10.5281/zenodo.30'
    fake = FakeHttp(
        files={'https://raw.githubusercontent.com/a/tool/HEAD/README.md': '10.5281/zenodo.30 then 10.5281/zenodo.10'},
        zenodo={
            'https://zenodo.org/api/records/30': (other, '@software{x, title={Someone Else}}'),
            'https://zenodo.org/api/records/10': (zenodo_record(), ZENODO_BIBTEX),
        },
    )
    assert run_find_citation(monkeypatch, fake)['title'] == 'a/tool: v0.5'


def test_releases_of_one_project_are_not_flagged_as_several_dois(monkeypatch):
    citation, _ = zenodo_citation(monkeypatch, '10.5281/zenodo.10 and 10.5281/zenodo.20', {'10': zenodo_record(), '20': zenodo_record()})
    assert citation['source'] == 'Zenodo'
    assert citation['review'] == []


def test_lone_unlinked_software_record_is_used(monkeypatch):
    citation, _ = zenodo_citation(monkeypatch, '10.5281/zenodo.10', {'10': zenodo_record(related=None)})
    assert citation['source'] == 'Zenodo'


def test_old_release_doi_uses_the_newest_release(monkeypatch):
    old = {**zenodo_record(), 'conceptrecid': '10', 'metadata': {'version': 'v0.1'}}
    newest = {**zenodo_record(), 'conceptrecid': '10'}
    fake = FakeHttp(
        files={README_URL: '10.5281/zenodo.5'},
        zenodo={
            'https://zenodo.org/api/records/5': (old, '@software{old, title={a/tool: v0.1}, doi={10.5281/zenodo.5}}'),
            'https://zenodo.org/api/records/10': (newest, ZENODO_BIBTEX),
        },
    )
    citation = run_find_citation(monkeypatch, fake)
    assert citation['title'] == 'a/tool: v0.5'
    assert citation['doi'] == '10.5281/zenodo.20'
    assert citation['zenodo_version'] == 'v0.5'
    assert '10.5281/zenodo.5' in citation['dois']


def test_any_zenodo_record_counts_as_citable(monkeypatch):
    citation, _ = zenodo_citation(monkeypatch, '10.5281/zenodo.10', {'10': zenodo_record(kind='dataset', related=None)})
    assert citation['source'] == 'Zenodo'
    assert citation['title'] == 'a/tool: v0.5'


def run_find_citation(monkeypatch, fake, files=None):
    monkeypatch.setattr(cite, 'http', fake)
    problems = []
    citation = cite.find_citation('https://github.com/a/tool', files or {}, problems)
    assert problems == []
    return citation


README_URL = 'https://raw.githubusercontent.com/a/tool/HEAD/README.md'
BADGE_URL = 'https://zenodo.org/badge/latestdoi/5282596'


def test_latestdoi_badge_resolves_to_a_zenodo_citation(monkeypatch):
    fake = FakeHttp(
        files={README_URL: f'<a href="{BADGE_URL}"><img src="https://zenodo.org/badge/5282596.svg"></a>'},
        redirects={BADGE_URL: 'https://doi.org/10.5281/zenodo.10'},
        zenodo={'https://zenodo.org/api/records/10': (zenodo_record(), ZENODO_BIBTEX)},
    )
    citation = run_find_citation(monkeypatch, fake)
    assert citation['source'] == 'Zenodo'
    assert citation['review'] == []


def test_latestdoi_badge_is_ignored_when_a_zenodo_doi_is_present(monkeypatch):
    fake = FakeHttp(
        files={README_URL: f'{BADGE_URL} and 10.5281/zenodo.10'},
        redirects={BADGE_URL: 'https://doi.org/10.5281/zenodo.99'},
        zenodo={'https://zenodo.org/api/records/10': (zenodo_record(), ZENODO_BIBTEX)},
    )
    assert run_find_citation(monkeypatch, fake)['dois'] == {'10.5281/zenodo.10', '10.5281/zenodo.20'}


def test_wrong_listed_readme_is_not_preferred(monkeypatch):
    fake = FakeHttp(files={
        README_URL: 'See 10.1000/real',
        'https://raw.githubusercontent.com/a/tool/HEAD/README.cn.md': 'See 10.1000/translation',
    })
    assert run_find_citation(monkeypatch, fake, {'readme': 'README.cn.md'})['doi'] == '10.1000/real'


def test_listed_plain_readme_is_used(monkeypatch):
    fake = FakeHttp(files={'https://raw.githubusercontent.com/a/tool/HEAD/README.txt': '10.1000/plain'})
    assert run_find_citation(monkeypatch, fake, {'readme': 'README.txt'})['doi'] == '10.1000/plain'


def test_bare_citation_file_with_bibtex_is_used_and_flagged(monkeypatch):
    fake = FakeHttp(files={'https://raw.githubusercontent.com/a/tool/HEAD/CITATION.txt': 'Please cite:\n@software{a, title={The Tool}, year=2019}\n'})
    citation = run_find_citation(monkeypatch, fake, {'citation': 'CITATION.txt'})
    assert citation['title'] == 'The Tool'
    assert citation['source'] == 'CITATION.txt'
    assert citation['review'] == ['CITATION.txt is not in a standard format, check it']
    assert cite.describe_source({**citation, 'dois': set()}) == 'has CITATION.txt'


def test_bare_citation_file_with_a_doi_is_used_and_flagged(monkeypatch):
    fake = FakeHttp(files={'https://raw.githubusercontent.com/a/tool/HEAD/CITATION': 'See 10.1000/tool'})
    citation = run_find_citation(monkeypatch, fake, {'citation': 'CITATION'})
    assert (citation['source'], citation['doi']) == ('CITATION', '10.1000/tool')
    assert len(citation['review']) == 1


def test_bare_citation_file_with_only_prose_is_flagged(monkeypatch):
    fake = FakeHttp(files={'https://raw.githubusercontent.com/a/tool/HEAD/CITATION.md': 'Please thank us.'})
    citation = run_find_citation(monkeypatch, fake, {'citation': 'CITATION.md'})
    assert citation['source'] == 'CITATION.md'
    assert citation['dois'] == set()
    assert citation['review']


def test_real_cff_wins_over_a_bare_pointer_file(monkeypatch):
    fake = FakeHttp(files={
        'https://raw.githubusercontent.com/a/tool/HEAD/CITATION': 'See https://github.com/a/tool/blob/main/a/CITATION',
        'https://raw.githubusercontent.com/a/tool/HEAD/CITATION.cff': 'title: Real\nauthors:\n  - name: Org\n',
    })
    citation = run_find_citation(monkeypatch, fake, {'citation': 'CITATION'})
    assert citation['title'] == 'Real'
    assert 'review' not in citation


def test_arxiv_is_flagged_alongside_a_doi(monkeypatch):
    fake = FakeHttp(files={README_URL: '10.1000/tool and https://arxiv.org/abs/1603.02754'})
    citation = run_find_citation(monkeypatch, fake)
    assert citation['doi'] == '10.1000/tool'
    assert citation['review'] == ['README mentions arXiv papers, check whether to cite them: arXiv:1603.02754']


def test_arxiv_alone_is_flagged_without_citation_data(monkeypatch):
    fake = FakeHttp(files={README_URL: 'See arXiv:1706.08359 and arxiv.org/pdf/1706.08359'})
    citation = run_find_citation(monkeypatch, fake)
    assert citation['source'] == 'README'
    assert citation['doi'] is None
    assert citation['review'] == ['README mentions arXiv papers, check whether to cite them: arXiv:1706.08359']
    assert cite.asked_to_be_cited(citation)


def test_arxiv_in_the_readme_is_flagged_for_a_bare_citation_file(monkeypatch):
    fake = FakeHttp(files={
        'https://raw.githubusercontent.com/a/tool/HEAD/CITATION': '@misc{a, title={Tool}}',
        README_URL: 'arXiv:1603.02754',
    })
    citation = run_find_citation(monkeypatch, fake, {'citation': 'CITATION'})
    assert len(citation['review']) == 2


TREE_URL = 'https://api.github.com/repos/a/tool/git/trees/HEAD'


def docs_http(pages, tree=None):
    base = 'https://raw.githubusercontent.com/a/tool/HEAD/'
    return FakeHttp(
        files={README_URL: 'Nothing here.', **{base + path: text for path, text in pages.items()}},
        trees={TREE_URL: tree if tree is not None else list(pages)},
    )


def test_docs_doi_counts_as_asking(monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'token')
    citation = run_find_citation(monkeypatch, docs_http({'docs/source/citing.rst': 'Cite 10.1000/docs'}))
    assert (citation['source'], citation['doi']) == ('docs', '10.1000/docs')
    assert citation['review'] == []
    assert cite.describe_source(citation) == 'DOI in docs: 10.1000/docs'


def test_docs_zenodo_link_is_citable(monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'token')
    fake = docs_http({'doc/index.rst': 'https://zenodo.org/records/10'})
    fake.zenodo = {'https://zenodo.org/api/records/10': (zenodo_record(), ZENODO_BIBTEX)}
    citation = run_find_citation(monkeypatch, fake)
    assert citation['source'] == 'Zenodo'
    assert cite.describe_source(citation) == 'Zenodo DOI in docs: 10.5281/zenodo.20'


def test_docs_citing_page_with_no_doi_is_flagged_with_a_link(monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'token')
    citation = run_find_citation(monkeypatch, docs_http({'doc/cite.rst': 'Please cite our paper.'}))
    assert citation['source'] == 'docs'
    assert citation['review'] == ['docs may say how to cite this project: https://github.com/a/tool/blob/HEAD/doc/cite.rst']


def test_docs_arxiv_is_flagged(monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'token')
    citation = run_find_citation(monkeypatch, docs_http({'docs/index.md': 'arXiv:1603.02754'}))
    assert citation['review'] == ['docs mentions arXiv papers, check whether to cite them: arXiv:1603.02754']


@pytest.mark.parametrize('path', ['docs/excitation.rst', 'src/citing.rst', 'docs/citing.py', 'docs/guide/index.rst'])
def test_unrelated_paths_are_not_docs_pages(monkeypatch, path):
    monkeypatch.setenv('GITHUB_TOKEN', 'token')
    assert run_find_citation(monkeypatch, docs_http({path: '10.1000/nope'})) is None


def test_docs_are_not_read_when_the_readme_has_a_doi(monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'token')
    fake = docs_http({'docs/citing.rst': '10.1000/docs'})
    fake.files[README_URL] = '10.1000/readme'
    assert run_find_citation(monkeypatch, fake)['doi'] == '10.1000/readme'


def test_docs_are_skipped_with_one_warning_without_a_token(monkeypatch, capsys):
    fake = docs_http({'docs/citing.rst': '10.1000/docs'})
    assert run_find_citation(monkeypatch, fake) is None
    assert run_find_citation(monkeypatch, fake) is None
    assert capsys.readouterr().err.count('set GITHUB_TOKEN') == 1


def test_record_missing_from_zenodo_falls_back_to_the_readme_doi(monkeypatch):
    citation, problems = zenodo_citation(monkeypatch, '10.5281/zenodo.10', {})
    assert citation['source'] == 'README'
    assert problems == []


def test_zenodo_outage_is_reported(monkeypatch):
    fake = FakeHttp(files={'https://raw.githubusercontent.com/a/tool/HEAD/README.md': '10.5281/zenodo.10'})
    original = fake.get
    monkeypatch.setattr(fake, 'get', lambda url, **kwargs: FakeResponse(503) if 'zenodo.org' in url else original(url, **kwargs))
    monkeypatch.setattr(cite, 'http', fake)
    dependency = cite.make_dependency('pypi', 'tool')
    problems = []
    packages = {cite.package_url(dependency): package('tool', 'https://github.com/a/tool')}
    citation = cite.build_citations([dependency], packages, problems)[0]
    assert citation['source'] == 'README'
    assert 'could not fetch 10.5281/zenodo.10 from Zenodo' in problems[0]


def test_citation_file_takes_priority_over_zenodo(monkeypatch):
    fake = FakeHttp(
        files={
            'https://raw.githubusercontent.com/a/tool/HEAD/CITATION.cff': 'title: Own Citation\nauthors:\n  - name: Org\n',
            'https://raw.githubusercontent.com/a/tool/HEAD/README.md': '10.5281/zenodo.10',
        },
        zenodo={'https://zenodo.org/api/records/10': (zenodo_record(), ZENODO_BIBTEX)},
    )
    monkeypatch.setattr(cite, 'http', fake)
    dependency = cite.make_dependency('pypi', 'tool')
    packages = {cite.package_url(dependency): package('tool', 'https://github.com/a/tool')}
    citation = cite.build_citations([dependency], packages, [])[0]
    assert citation['title'] == 'Own Citation'


def test_fallback_title_keeps_the_registry_spelling():
    dependency = cite.make_dependency('pypi', 'ruamel.yaml', '0.18.6')
    packages = {cite.package_url(dependency): package('ruamel.yaml', None)}
    citation = cite.build_citations([dependency], packages, [])[0]
    assert citation['package'] == 'ruamel-yaml'
    assert citation['title'] == 'ruamel.yaml'


def test_generate_overwrites_output_and_uses_published_citation(tmp_path, monkeypatch, no_sleep):
    project(tmp_path)
    output = tmp_path / 'out.bib'
    output.write_text('stale')
    monkeypatch.setattr(cite, 'http', audit_http())
    assert cite.main([str(tmp_path), '--output', str(output)]) == 0
    text = output.read_text()
    assert 'stale' not in text
    assert 'title = {Cited Tool}' in text
    assert 'doi = {10.1000/cited}' in text
    assert 'title = {notcited}' in text
