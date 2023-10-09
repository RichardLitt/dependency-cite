# Python Package Citation Generator

This script automatically generates citations in BibTeX format for Python packages listed in a `requirements.txt` file. It fetches package details, including the GitHub repository (if available) and contributors, from PyPI and GitHub.

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

---

Please adjust the README as needed based on your script's name, additional details, or any other specifics that might be relevant to your context.
