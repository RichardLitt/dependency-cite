# Python Package Citation Generator

This script automatically generates citations in BibTeX format for Python packages listed in a `requirements.txt` file. It fetches package details, including the GitHub repository (if available) and contributors, from PyPI and GitHub.

## About this package

Currently, research software is undercited, and therefore underrecognized by researchers as a valuable output for science. There have been a lot of strides in the past few decades to changing this, running from the software format in Bibtex, to the CITATION.cff file that can be embedded in repositories, to linking software with DOIs and OrcidIDs on Zenodo, to publishing software on the Journal of Open Source Software. 

However, there remains a problem of how to cite all software dependencies used in your research. Currently, most software citations are for charismatic, large projects that are high up the dependency chain, which the author wants to cite. "We used this package <package>", for instance. This ignores the work of transitive dependencies for those packages, and by extension the work of software engineers or researchers who build those packages. As well, packages which are not included in a manifest are rarely cited - for instance, tech used in the research process that wasn't related to the final output. 

To change this, I've started building a software suite that looks up the dependencies for the project you are using, and cites them. Ideally, it cites the packages using citation formats that the authors had intended to be cited - CITATION.cff files, Zenodo links, and so on. If not, it grabs the best guess of the authors of the package, and creates a new citation for that version. 

This all came out of an idea that started at the ReSA funders workship in Montréal in September, 2023. I posted on the JOSS forum about my idea, here: [Ask researchers to cite software they use before allowing publication in JOSS](https://github.com/openjournals/joss/issues/1277). 

I have now coded a bit of the work, and hope to expand it futher. Contributions and thoughts would be most welcome. 

## Features

- Extract package names and versions from `requirements.txt`.
- Fetch package details and GitHub URLs from PyPI.
- Retrieve contributor information for the package from its GitHub repository.
- Generate BibTeX entries for each package and write them to `output.bib`.

## Prerequisites

- Python 3.6 or newer.
- `requests` library: Used to make API requests to PyPI and GitHub.
  
  Install with:
  ```bash
  pip install requests
  ```

- A GitHub token: To avoid rate limits when accessing the GitHub API. Set it as an environment variable named `GITHUB_TOKEN`.

## Usage

1. Ensure your `requirements.txt` file is in the same directory as the script. It should list packages with their versions, e.g.,

   ```
   numpy==1.21.0
   scipy==1.7.0
   ```

2. Run the script:

   ```bash
   python script_name.py
   ```

   Replace `script_name.py` with the actual name of the script.

3. Check the generated `output.bib` file for BibTeX formatted citations.

## Output Format

The script will generate citations in the following format:

```
@software{package_name,
  author = {Author1 and Author2 and ...},
  title = {Package Title},
  url = {https://github.com/user/repo},
  version = {version_number},
  date = {publication_year},
}
```

## Notes

- Packages without a linked GitHub repository or without an `==` version specifier in `requirements.txt` might not be processed.
- If the script encounters rate-limiting issues with GitHub's API, ensure you've set the `GITHUB_TOKEN` environment variable with a valid GitHub token.

## Contributing

If you'd like to contribute to this project, please open an issue or submit a pull request.

## License

[MIT](LICENSE).
