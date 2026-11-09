# Dependency Citation Checker and Generator

This script finds all of a project's dependencies, including transitive ones, and either checks whether they have been cited or generates citations for them. It uses [ecosyste.ms](https://ecosyste.ms) for package data and prefers the citation each project asks for in its own `CITATION.cff` or `CITATION.bib`.

## About this package

Currently, research software is undercited, and therefore underrecognized by researchers as a valuable output for science. There have been a lot of strides in the past few decades to changing this, running from the software format in Bibtex, to the CITATION.cff file that can be embedded in repositories, to linking software with DOIs and OrcidIDs on Zenodo, to publishing software on the Journal of Open Source Software. 

However, there remains a problem of how to cite all software dependencies used in your research. Currently, most software citations are for charismatic, large projects that are high up the dependency chain, which the author wants to cite. "We used this package <package>", for instance. This ignores the work of transitive dependencies for those packages, and by extension the work of software engineers or researchers who build those packages. As well, packages which are not included in a manifest are rarely cited - for instance, tech used in the research process that wasn't related to the final output. 

To change this, I've started building a software suite that looks up the dependencies for the project you are using, and cites them. Ideally, it cites the packages using citation formats that the authors had intended to be cited - CITATION.cff files, Zenodo links, and so on. If not, it grabs the best guess of the authors of the package, and creates a new citation for that version. 

This all came out of an idea that started at the ReSA funders workship in Montréal in September, 2023. I posted on the JOSS forum about my idea, here: [Ask researchers to cite software they use before allowing publication in JOSS](https://github.com/openjournals/joss/issues/1277). 

I have now coded a bit of the work, and hope to expand it futher. Contributions and thoughts would be most welcome. 

## Features

- Find dependencies across Python, npm, Cargo, RubyGems and Go projects, or read them from an SBOM.
- Resolve transitive dependencies with ecosyste.ms when a project has no lockfile.
- Use each dependency's own `CITATION.cff` or `CITATION.bib` where it has one, and a best guess of its authors otherwise.
- Check a `CITATION.cff` or BibTeX file for dependencies that have not been cited.
- Write citations as BibTeX or as CFF `references`.

## Prerequisites

- Python 3.11 or newer.
- The packages in `requirements.txt`:

  ```bash
  pip install -r requirements.txt
  ```

- Optionally, a GitHub token, set as an environment variable named `GITHUB_TOKEN`. With a token, dependencies that have no citation file credit their GitHub contributors. Without one, they credit their registry maintainers or repository owner.

## Usage

Generate citations for every dependency of the project in the current directory, and write them to `output.bib`. This replaces any existing `output.bib`:

```bash
python cite_dependencies.py
```

Check that every dependency is listed in the `references` of the project's `CITATION.cff`:

```bash
python cite_dependencies.py path/to/project --check
```

Check a paper's bibliography instead, and write ready-to-paste entries for whatever is missing:

```bash
python cite_dependencies.py --check --cited-in paper.bib --output missing.bib
```

| Option | Description |
| --- | --- |
| `--check` | Report which dependencies are not cited, instead of generating citations. |
| `--cited-in FILE` | The `.cff` or `.bib` file to check against. Defaults to `CITATION.cff` in the project. |
| `--output FILE` | Where to write citations: `.bib` for BibTeX, anything else for CFF `references`. With `--check`, only missing dependencies are written. |
| `--sbom FILE` | Read dependencies from a CycloneDX or SPDX JSON SBOM instead of the project files. |
| `--json` | Print the `--check` report as JSON. |
| `--asked-to-be-cited` | Only include dependencies that have asked to be cited (see below). Applies to the report, the exit code and any written citations. |

`--check` exits with `0` if everything is cited, `1` if any dependency is missing, and `2` if nothing is missing but the check could not be completed, so it can be used in CI.

## Where dependencies come from

| Ecosystem | Complete lockfile | Expanded with the ecosyste.ms resolver |
| --- | --- | --- |
| Python | `uv.lock`, `poetry.lock` | `requirements.txt`, `pyproject.toml` |
| npm | `package-lock.json` (version 2 or later) | `package.json` |
| Cargo | `Cargo.lock` | `Cargo.toml` |
| RubyGems | `Gemfile.lock` | |
| Go | `go.mod` | |

A lockfile takes priority over a manifest for the same ecosystem. Lockfiles and `package.json` include development dependencies. Use `--sbom` for anything else.

## How a dependency counts as cited

A dependency is cited if a reference has the same DOI as the dependency's own citation, points to the same repository, or has the same title as the package or its citation. The report says which of these matched. A package in a monorepo is covered by a citation of that repository.

## What counts as asking to be cited

Each repository is checked in this order, stopping at the first citable result:

| Signal | Result |
| --- | --- |
| `CITATION.cff` or `CITATION.bib` | Citable, using that file. |
| Another citation file, such as `CITATION` or `CITATION.txt` | Flagged, using any BibTeX or DOI inside it. |
| Zenodo link or badge in the README | Citable, using Zenodo's BibTeX. |
| Any other DOI in the README | Citable, using the DOI. |
| Zenodo link or DOI in the docs | As for the README. Needs `GITHUB_TOKEN`. |
| Citing page in the docs with no DOI | Flagged, with a link to the page. Needs `GITHUB_TOKEN`. |
| arXiv paper in the README or docs | Always flagged, even alongside a DOI. |

Docs means pages under `doc/` or `docs/` with a name like `citing` or `citation`, and the docs index page.

Flagged dependencies are for a person to review. They appear in a "Needs review" list when the script runs, as a `% NEEDS REVIEW` comment in BibTeX, as `notes` in CFF, and under `needs_review` and `review` in JSON.

## Known limitations

### Finding dependencies

- Without a lockfile, only exact pins in your own manifest are respected. Everything below them resolves to the latest version, so the dependencies checked may differ from what you would install.
- `-r` includes in `requirements.txt`, optional dependencies and dependency groups in `pyproject.toml`, and optional extras of dependencies are not followed.
- An SBOM is used as it is, and nothing is added to it.
- `go.mod` files from before Go 1.17 list only part of the dependency tree, but are treated as complete.
- If the resolver fails for a dependency, whatever that dependency pulls in is missing from the results. The report says the check is incomplete.
- `uv.lock`, `poetry.lock`, `package-lock.json`, `pyproject.toml`, `Cargo.lock`, `Cargo.toml`, `Gemfile.lock` and SPDX input have unit tests at most, and have not been run on a real project's files.

### Reading citations

- Any DOI in the docs counts as asking to be cited, including on a docs index page, where it may only be a reference. NetworkX's `doc/index.rst`, for example, lists a paper it merely references, so NetworkX gets that paper's DOI and is not flagged.
- The script compares dependencies against your own citations. It does not check whether your dependencies have cited their own dependencies.

- Citation files are read from each repository's default branch, so they may be newer than the version you use.
- Citation files and READMEs can only be fetched from GitHub and GitLab.
- A Zenodo link is trusted without checking that the record belongs to the project. Among several Zenodo records, the one linked to the repository is used, otherwise the first that resolves.
- A Zenodo citation always describes the most recent release on Zenodo, even if the link is to an older one. This may not be the version you use.
- A DOI may identify something other than the project, such as a paper it builds on.
- If a page has several DOIs, the dependency is flagged. Its citation only has a DOI if it came from a Zenodo record.
- Authors in best-guess citations are not authoritative, and maintainers are not always the authors. Check them before publishing.

## Contributing

If you'd like to contribute to this project, please open an issue or submit a pull request.

## License

[MIT](LICENSE).
