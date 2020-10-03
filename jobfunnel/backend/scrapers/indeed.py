"""Scraper designed to get jobs from www.indeed.X
"""
import re
from concurrent.futures import ThreadPoolExecutor, wait
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from requests import Session

from jobfunnel.backend import Job
from jobfunnel.backend.scrapers.base import (BaseCANEngScraper, BaseScraper,
                                             BaseUSAEngScraper,
                                             BaseUKEngScraper)
from jobfunnel.backend.tools.filters import JobFilter
from jobfunnel.backend.tools.tools import calc_post_date_from_relative_str
from jobfunnel.resources import MAX_CPU_WORKERS, JobField, Remoteness

# pylint: disable=using-constant-test,unused-import
if False:  # or typing.TYPE_CHECKING  if python3.5.3+
    from jobfunnel.config import JobFunnelConfigManager
# pylint: enable=using-constant-test,unused-import

ID_REGEX = re.compile(r'id=\"sj_([a-zA-Z0-9]*)\"')
MAX_RESULTS_PER_INDEED_PAGE = 50
# NOTE: these magic strings stick for both the US and CAN indeed websites...
FULLY_REMOTE_MAGIC_STRING = "&remotejob=032b3046-06a3-4876-8dfd-474eb5e7ed11"
COVID_REMOTE_MAGIC_STRING = "&remotejob=7e3167e4-ccb4-49cb-b761-9bae564a0a63"
REMOTENESS_TO_QUERY = {
    Remoteness.IN_PERSON: '',
    Remoteness.TEMPORARILY_REMOTE: COVID_REMOTE_MAGIC_STRING,
    Remoteness.PARTIALLY_REMOTE: '',
    Remoteness.FULLY_REMOTE: FULLY_REMOTE_MAGIC_STRING,
    Remoteness.ANY: '',
}
REMOTENESS_STR_MAP = {
    'remote': Remoteness.FULLY_REMOTE,
    'temporarily remote': Remoteness.TEMPORARILY_REMOTE,
}


class BaseIndeedScraper(BaseScraper):
    """Scrapes jobs from www.indeed.X
    """

    def __init__(self, session: Session, config: 'JobFunnelConfigManager',
                 job_filter: JobFilter) -> None:
        """Init that contains indeed specific stuff
        """
        super().__init__(session, config, job_filter)
        self.max_results_per_page = MAX_RESULTS_PER_INDEED_PAGE
        self.query = '+'.join(self.config.search_config.keywords)

        # Log if we can't do their remoteness query (Indeed only has 2 lvls.)
        if self.config.search_config.remoteness == Remoteness.PARTIALLY_REMOTE:
            self.logger.warning("Indeed does not support PARTIALLY_REMOTE jobs")

    @property
    def job_get_fields(self) -> str:
        """Call self.get(...) for the JobFields in this list when scraping a Job

        Override this as needed.
        """
        return [
            JobField.TITLE, JobField.COMPANY, JobField.LOCATION,
            JobField.KEY_ID, JobField.TAGS, JobField.POST_DATE,
            JobField.REMOTENESS, JobField.WAGE,
        ]

    @property
    def job_set_fields(self) -> str:
        """Call self.set(...) for the JobFields in this list when scraping a Job

        NOTE: Since this passes the Job we are updating, the order of this list
        matters if set fields rely on each-other.

        Override this as needed.
        """
        return [JobField.RAW, JobField.URL, JobField.DESCRIPTION]

    @property
    def delayed_get_set_fields(self) -> str:
        """Delay execution when getting /setting any of these attributes of a
        job.

        Override this as needed.
        """
        return [JobField.RAW]

    @property
    def high_priority_get_set_fields(self) -> List[JobField]:
        """These get() and/or set() fields will be populated first.
        """
        return [JobField.URL]

    @property
    def headers(self) -> Dict[str, str]:
        """Session header for indeed.X
        """
        return {
            'accept': 'text/html,application/xhtml+xml,application/xml;'
            'q=0.9,image/webp,*/*;q=0.8',
            'accept-encoding': 'gzip, deflate, sdch, br',
            'accept-language': 'en-GB,en-US;q=0.8,en;q=0.6',
            'referer':
                f'https://www.indeed.{self.config.search_config.domain}/',
            'upgrade-insecure-requests': '1',
            'user-agent': self.user_agent,
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive'
        }

    def _get_n_pages(self, max_pages: Optional[int] = None) -> int:
        """Calculates the number of pages of job listings to be scraped.

        i.e. your search yields 230 results at 50 res/page -> 5 pages of jobs

        Args:
			max_pages: the maximum number of pages to be scraped.
        Returns:
            The number of pages to be scraped.
        """
        # Get the html data, initialize bs4 with lxml
        request_html = self._get_search_page()
        search_url = request_html.url
        self.logger.debug(
            "Got base search results page: %s", search_url
        )
        query_resp = BeautifulSoup(request_html.text, self.config.bs4_parser)

        # obtain the page number. 
        # TODO: this process is not robust at all.
        num_res = query_resp.find(id='searchCountPages')
        if not num_res:
            raise ValueError(
                "Unable to identify number of pages of results for query: {}"
                " Please ensure linked page contains results, you may have"
                " provided a city for which there are no results within this"
                " province or state.".format(search_url)
            )
        num_res = num_res.contents[0].strip()
        num_res = int(re.findall(r'f (\d+) ', num_res.replace(',', ''))[0])
        number_of_pages = int(ceil(num_res / self.max_results_per_page))

        if not max_pages:
            return number_of_pages
        elif number_of_pages < max_pages:
            return number_of_pages
        else:
            return max_pages

    def get(self, parameter: JobField, soup: BeautifulSoup) -> Any:
        """Get a single job attribute from a soup object by JobField
        """
        if parameter == JobField.TITLE:
            return soup.find(
                'a', attrs={'data-tn-element': 'jobTitle'}
            ).text.strip()
        elif parameter == JobField.COMPANY:
            return soup.find('span', attrs={'class': 'company'}).text.strip()
        elif parameter == JobField.LOCATION:
            return soup.find('span', attrs={'class': 'location'}).text.strip()
        elif parameter == JobField.TAGS:
            # tags may not be on page and that's ok.
            table_soup = soup.find(
                'table', attrs={'class': 'jobCardShelfContainer'}
            )
            if table_soup:
                return [
                    td.text.strip() for td in table_soup.find_all(
                        'td', attrs={'class': 'jobCardShelfItem'}
                    )
                ]
            else:
                return []
        elif parameter == JobField.REMOTENESS:
            remote_field = soup.find('span', attrs={'class': 'remote'})
            if remote_field:
                remoteness_str = remote_field.text.strip().lower()
                if remoteness_str in REMOTENESS_STR_MAP:
                    return REMOTENESS_STR_MAP[remoteness_str]
            return Remoteness.UNKNOWN
        elif parameter == JobField.WAGE:
            # We may not be able to obtain a wage
            potential = soup.find('span', attrs={'class': 'salaryText'})
            if potential:
                return potential.text.strip()
            else:
                return ''
        elif parameter == JobField.POST_DATE:
            return calc_post_date_from_relative_str(
                soup.find('span', attrs={'class': 'date'}).text.strip()
            )
        elif parameter == JobField.KEY_ID:
            return ID_REGEX.findall(
                str(
                    soup.find(
                        'a', attrs={'class': 'sl resultLink save-job-link'}
                    )
                )
            )[0]
        else:
            raise NotImplementedError(f"Cannot get {parameter.name}")

    def set(self, parameter: JobField, job: Job, soup: BeautifulSoup) -> None:
        """Set a single job attribute from a soup object by JobField
        NOTE: URL is high-priority, since we need it to get RAW.
        """
        if parameter == JobField.RAW:
            job._raw_scrape_data = BeautifulSoup(
                self.session.get(job.url).text, self.config.bs4_parser
            )
        elif parameter == JobField.DESCRIPTION:
            assert job._raw_scrape_data
            job.description = job._raw_scrape_data.find(
                id='jobDescriptionText'
            ).text.strip()
        elif parameter == JobField.URL:
            assert job.key_id
            job.url = (
                f"http://www.indeed.{self.config.search_config.domain}/"
                f"viewjob?jk={job.key_id}"
            )
        else:
            raise NotImplementedError(f"Cannot set {parameter.name}")

    def _parse_job_listings_to_bs4(self, page_soup: BeautifulSoup
                                   ) -> List[BeautifulSoup]:
        """Parse a page of job listings HTML text into job soups
        """
        return page_soup.find_all('div', attrs={'data-tn-component': 'organicJob'})

    def _get_search_stem_url(self) -> str:
        """Get the search stem url for initial search."""
        return f"https://www.indeed.{self.config.search_config.domain}/jobs"

    def _get_search_args(self) -> Dict[str, str]:
        """Get all arguments used for the search query."""
        return {
            'q': self.query,
            'l': f"{self.config.search_config.city.replace(' ', '+',)}%2C+{self.config.search_config.province_or_state.upper()}",
            'radius': self._quantize_radius(self.config.search_config.radius),
            'limit': self.max_results_per_page,
            'filter': f"{int(self.config.search_config.return_similar_results)}{REMOTENESS_TO_QUERY[self.config.search_config.remoteness]}",
        }

    def _get_page_query(self, page: int) -> Tuple[str, str]:
        """Return query parameter and value for specific provider."""
        return ('start', int(page * self.max_results_per_page))

    def _quantize_radius(self, radius: int) -> int:
        """Quantizes the user input radius to a valid radius value into:
        5, 10, 15, 25, 50, 100, and 200 kilometers or miles.
        TODO: implement with numpy instead of if/else cases.
        """
        if radius < 5:
            radius = 0
        elif 5 <= radius < 10:
            radius = 5
        elif 10 <= radius < 15:
            radius = 10
        elif 15 <= radius < 25:
            radius = 15
        elif 25 <= radius < 50:
            radius = 25
        elif 50 <= radius < 100:
            radius = 50
        elif radius >= 100:
            radius = 100
        return radius


class IndeedScraperCANEng(BaseIndeedScraper, BaseCANEngScraper):
    """Scrapes jobs from www.indeed.ca
    """


class IndeedScraperUSAEng(BaseIndeedScraper, BaseUSAEngScraper):
    """Scrapes jobs from www.indeed.com
    """


class IndeedScraperUKEng(BaseIndeedScraper, BaseUKEngScraper):
    """Scrapes jobs from www.indeed.co.uk
    """
    def _get_search_args(self) -> Dict[str, str]:
        """Get all arguments used for the search query."""
        # first get arguments from parent class, then override the location
        args = super()._get_search_args()
        args['l'] = self.config.search_config.city.replace(' ', '+',)
