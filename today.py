import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib


# ============================================================
# GitHub API configuration
# ============================================================

HEADERS = {
    "authorization": "token " + os.environ["ACCESS_TOKEN"]
}

USER_NAME = os.environ["USER_NAME"]

QUERY_COUNT = {
    "user_getter": 0,
    "follower_getter": 0,
    "graph_repos_stars": 0,
    "recursive_loc": 0,
    "graph_commits": 0,
    "loc_query": 0
}


# ============================================================
# Utility functions
# ============================================================

def daily_readme(account_date):
    """
    Returns the length of time since the GitHub account was created.

    Example:
        5 years, 3 months, 12 days
    """

    created = datetime.datetime.fromisoformat(
        account_date.replace("Z", "+00:00")
    )

    now = datetime.datetime.now(datetime.timezone.utc)

    diff = relativedelta.relativedelta(now, created)

    return "{} {}, {} {}, {} {}".format(
        diff.years,
        "year" + format_plural(diff.years),
        diff.months,
        "month" + format_plural(diff.months),
        diff.days,
        "day" + format_plural(diff.days)
    )


def format_plural(unit):
    """
    Returns 's' when the number is not 1.

    Example:
        1 day
        5 days
    """

    return "s" if unit != 1 else ""


def simple_request(func_name, query, variables):
    """
    Sends a request to GitHub's GraphQL API.
    """

    request = requests.post(
        "https://api.github.com/graphql",
        json={
            "query": query,
            "variables": variables
        },
        headers=HEADERS
    )

    if request.status_code == 200:
        return request

    raise Exception(
        func_name,
        " has failed with",
        request.status_code,
        request.text,
        QUERY_COUNT
    )


# ============================================================
# GitHub contribution statistics
# ============================================================

def graph_commits(start_date, end_date):
    """
    Returns the user's GitHub contribution count
    between start_date and end_date.
    """

    query_count("graph_commits")

    query = """
    query(
        $start_date: DateTime!,
        $end_date: DateTime!,
        $login: String!
    ) {
        user(login: $login) {
            contributionsCollection(
                from: $start_date,
                to: $end_date
            ) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }
    """

    variables = {
        "start_date": start_date,
        "end_date": end_date,
        "login": USER_NAME
    }

    request = simple_request(
        graph_commits.__name__,
        query,
        variables
    )

    return int(
        request.json()["data"]["user"]
        ["contributionsCollection"]
        ["contributionCalendar"]
        ["totalContributions"]
    )


def graph_repos_stars(
    count_type,
    owner_affiliation,
    cursor=None
):
    """
    Returns repository, star, or contribution counts.
    """

    query_count("graph_repos_stars")

    query = """
    query(
        $owner_affiliation: [RepositoryAffiliation],
        $login: String!,
        $cursor: String
    ) {
        user(login: $login) {
            repositories(
                first: 100,
                after: $cursor,
                ownerAffiliations: $owner_affiliation
            ) {
                totalCount

                edges {
                    node {
                        ... on Repository {
                            nameWithOwner

                            stargazers {
                                totalCount
                            }
                        }
                    }
                }

                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }
    """

    variables = {
        "owner_affiliation": owner_affiliation,
        "login": USER_NAME,
        "cursor": cursor
    }

    request = simple_request(
        graph_repos_stars.__name__,
        query,
        variables
    )

    repositories = request.json()["data"]["user"]["repositories"]

    if count_type == "repos":
        return repositories["totalCount"]

    elif count_type == "stars":
        return stars_counter(repositories["edges"])

    raise ValueError("Unknown count_type: " + count_type)


def stars_counter(data):
    """
    Counts total stars on repositories owned by the user.
    """

    total_stars = 0

    for node in data:
        total_stars += node["node"]["stargazers"]["totalCount"]

    return total_stars


# ============================================================
# Lines of Code calculation
# ============================================================

def recursive_loc(
    owner,
    repo_name,
    data,
    cache_comment,
    addition_total=0,
    deletion_total=0,
    my_commits=0,
    cursor=None
):
    """
    Fetches repository commits using cursor pagination.

    Only commits authored by the current GitHub user
    are counted toward lines of code.
    """

    query_count("recursive_loc")

    query = """
    query(
        $repo_name: String!,
        $owner: String!,
        $cursor: String
    ) {
        repository(
            name: $repo_name,
            owner: $owner
        ) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(
                            first: 100,
                            after: $cursor
                        ) {
                            totalCount

                            edges {
                                node {
                                    ... on Commit {
                                        committedDate

                                        author {
                                            user {
                                                id
                                            }
                                        }

                                        deletions
                                        additions
                                    }
                                }
                            }

                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }
    """

    variables = {
        "repo_name": repo_name,
        "owner": owner,
        "cursor": cursor
    }

    request = requests.post(
        "https://api.github.com/graphql",
        json={
            "query": query,
            "variables": variables
        },
        headers=HEADERS
    )

    if request.status_code == 200:

        repository = request.json()["data"]["repository"]

        if repository is not None:

            branch = repository["defaultBranchRef"]

            if branch is not None:

                return loc_counter_one_repo(
                    owner,
                    repo_name,
                    data,
                    cache_comment,
                    branch["target"]["history"],
                    addition_total,
                    deletion_total,
                    my_commits
                )

        return 0

    force_close_file(data, cache_comment)

    if request.status_code == 403:
        raise Exception(
            "Too many requests in a short amount of time!"
        )

    raise Exception(
        "recursive_loc() has failed with",
        request.status_code,
        request.text,
        QUERY_COUNT
    )


def loc_counter_one_repo(
    owner,
    repo_name,
    data,
    cache_comment,
    history,
    addition_total,
    deletion_total,
    my_commits
):
    """
    Adds additions/deletions from commits authored by the user.
    """

    for node in history["edges"]:

        commit = node["node"]

        author = commit.get("author")

        if author is None:
            continue

        user = author.get("user")

        if user is None:
            continue

        if user["id"] == OWNER_ID:

            my_commits += 1
            addition_total += commit["additions"]
            deletion_total += commit["deletions"]

    if (
        history["edges"] == []
        or not history["pageInfo"]["hasNextPage"]
    ):
        return (
            addition_total,
            deletion_total,
            my_commits
        )

    return recursive_loc(
        owner,
        repo_name,
        data,
        cache_comment,
        addition_total,
        deletion_total,
        my_commits,
        history["pageInfo"]["endCursor"]
    )


def loc_query(
    owner_affiliation,
    comment_size=0,
    force_cache=False,
    cursor=None,
    edges=None
):
    """
    Queries all repositories available to the user.

    Repository LOC data is cached so we don't have to
    recalculate every repository on every run.
    """

    query_count("loc_query")

    if edges is None:
        edges = []

    query = """
    query(
        $owner_affiliation: [RepositoryAffiliation],
        $login: String!,
        $cursor: String
    ) {
        user(login: $login) {

            repositories(
                first: 60,
                after: $cursor,
                ownerAffiliations: $owner_affiliation
            ) {

                edges {

                    node {

                        ... on Repository {

                            nameWithOwner

                            defaultBranchRef {

                                target {

                                    ... on Commit {

                                        history {
                                            totalCount
                                        }

                                    }

                                }

                            }

                        }

                    }

                }

                pageInfo {
                    endCursor
                    hasNextPage
                }

            }

        }
    }
    """

    variables = {
        "owner_affiliation": owner_affiliation,
        "login": USER_NAME,
        "cursor": cursor
    }

    request = simple_request(
        loc_query.__name__,
        query,
        variables
    )

    repositories = request.json()["data"]["user"]["repositories"]

    edges += repositories["edges"]

    if repositories["pageInfo"]["hasNextPage"]:

        return loc_query(
            owner_affiliation,
            comment_size,
            force_cache,
            repositories["pageInfo"]["endCursor"],
            edges
        )

    return cache_builder(
        edges,
        comment_size,
        force_cache
    )


def cache_builder(
    edges,
    comment_size,
    force_cache
):
    """
    Checks cached repository data.

    If a repository has new commits,
    its LOC is recalculated.
    """

    cached = True

    os.makedirs("cache", exist_ok=True)

    filename = (
        "cache/"
        + hashlib.sha256(
            USER_NAME.encode("utf-8")
        ).hexdigest()
        + ".txt"
    )

    try:

        with open(filename, "r") as f:
            data = f.readlines()

    except FileNotFoundError:

        data = []

        if comment_size > 0:

            for _ in range(comment_size):
                data.append(
                    "This line is a comment block.\n"
                )

        with open(filename, "w") as f:
            f.writelines(data)

    repository_data = data[comment_size:]

    if (
        len(repository_data) != len(edges)
        or force_cache
    ):

        cached = False

        flush_cache(
            edges,
            filename,
            comment_size
        )

        with open(filename, "r") as f:
            data = f.readlines()

    cache_comment = data[:comment_size]

    data = data[comment_size:]

    for index in range(len(edges)):

        parts = data[index].split()

        repo_hash = parts[0]
        commit_count = parts[1]

        current_repo_hash = hashlib.sha256(
            edges[index]["node"]["nameWithOwner"]
            .encode("utf-8")
        ).hexdigest()

        if repo_hash == current_repo_hash:

            try:

                current_commit_count = (
                    edges[index]["node"]
                    ["defaultBranchRef"]
                    ["target"]
                    ["history"]
                    ["totalCount"]
                )

                if int(commit_count) != current_commit_count:

                    owner, repo_name = (
                        edges[index]["node"]
                        ["nameWithOwner"]
                        .split("/")
                    )

                    loc = recursive_loc(
                        owner,
                        repo_name,
                        data,
                        cache_comment
                    )

                    data[index] = (
                        repo_hash
                        + " "
                        + str(current_commit_count)
                        + " "
                        + str(loc[2])
                        + " "
                        + str(loc[0])
                        + " "
                        + str(loc[1])
                        + "\n"
                    )

            except TypeError:

                data[index] = (
                    repo_hash
                    + " 0 0 0 0\n"
                )

    with open(filename, "w") as f:

        f.writelines(cache_comment)
        f.writelines(data)

    loc_add = 0
    loc_del = 0

    for line in data:

        loc = line.split()

        loc_add += int(loc[3])
        loc_del += int(loc[4])

    return [
        loc_add,
        loc_del,
        loc_add - loc_del,
        cached
    ]


def flush_cache(
    edges,
    filename,
    comment_size
):
    """
    Rebuilds the repository cache.
    """

    old_comment = []

    try:

        with open(filename, "r") as f:

            if comment_size > 0:
                old_comment = f.readlines()[:comment_size]

    except FileNotFoundError:
        pass

    with open(filename, "w") as f:

        f.writelines(old_comment)

        for node in edges:

            repo_hash = hashlib.sha256(
                node["node"]["nameWithOwner"]
                .encode("utf-8")
            ).hexdigest()

            f.write(
                repo_hash
                + " 0 0 0 0\n"
            )


def force_close_file(
    data,
    cache_comment
):
    """
    Saves partially calculated cache data if an API
    request fails.
    """

    os.makedirs("cache", exist_ok=True)

    filename = (
        "cache/"
        + hashlib.sha256(
            USER_NAME.encode("utf-8")
        ).hexdigest()
        + ".txt"
    )

    with open(filename, "w") as f:

        f.writelines(cache_comment)
        f.writelines(data)

    print(
        "There was an error while writing to the file."
    )


# ============================================================
# User information
# ============================================================

def user_getter(username):
    """
    Returns the user's GitHub ID and account creation date.
    """

    query_count("user_getter")

    query = """
    query($login: String!) {

        user(login: $login) {

            id
            createdAt

        }

    }
    """

    variables = {
        "login": username
    }

    request = simple_request(
        user_getter.__name__,
        query,
        variables
    )

    user = request.json()["data"]["user"]

    return {
        "id": user["id"]
    }, user["createdAt"]


def follower_getter(username):
    """
    Returns the user's follower count.
    """

    query_count("follower_getter")

    query = """
    query($login: String!) {

        user(login: $login) {

            followers {
                totalCount
            }

        }

    }
    """

    request = simple_request(
        follower_getter.__name__,
        query,
        {
            "login": username
        }
    )

    return int(
        request.json()["data"]["user"]
        ["followers"]
        ["totalCount"]
    )


# ============================================================
# SVG modification
# ============================================================

def svg_overwrite(
    filename,
    age_data,
    commit_data,
    star_data,
    repo_data,
    contrib_data,
    follower_data,
    loc_data
):
    """
    Replaces the values inside the SVG.
    """

    tree = etree.parse(filename)

    root = tree.getroot()

    justify_format(
        root,
        "commit_data",
        commit_data,
        22
    )

    justify_format(
        root,
        "star_data",
        star_data,
        14
    )

    justify_format(
        root,
        "repo_data",
        repo_data,
        6
    )

    justify_format(
        root,
        "contrib_data",
        contrib_data
    )

    justify_format(
        root,
        "follower_data",
        follower_data,
        10
    )

    justify_format(
        root,
        "loc_data",
        loc_data[2],
        9
    )

    justify_format(
        root,
        "loc_add",
        loc_data[0]
    )

    justify_format(
        root,
        "loc_del",
        loc_data[1],
        7
    )

    justify_format(
        root,
        "age_data",
        age_data
    )

    tree.write(
        filename,
        encoding="utf-8",
        xml_declaration=True
    )


def justify_format(
    root,
    element_id,
    new_text,
    length=0
):
    """
    Updates an SVG value and adjusts the dots
    before it so the formatting remains aligned.
    """

    if isinstance(new_text, int):

        new_text = "{:,}".format(new_text)

    new_text = str(new_text)

    find_and_replace(
        root,
        element_id,
        new_text
    )

    just_len = max(
        0,
        length - len(new_text)
    )

    if just_len <= 2:

        dot_map = {
            0: "",
            1: " ",
            2: ". "
        }

        dot_string = dot_map[just_len]

    else:

        dot_string = (
            " "
            + ("." * just_len)
            + " "
        )

    find_and_replace(
        root,
        element_id + "_dots",
        dot_string
    )


def find_and_replace(
    root,
    element_id,
    new_text
):
    """
    Finds an SVG element by ID and replaces its text.
    """

    element = root.find(
        ".//*[@id='{}']".format(element_id)
    )

    if element is not None:
        element.text = new_text


# ============================================================
# Commit counter
# ============================================================

def commit_counter(comment_size):
    """
    Counts commits authored by the user.

    The number is obtained from the repository cache.
    """

    filename = (
        "cache/"
        + hashlib.sha256(
            USER_NAME.encode("utf-8")
        ).hexdigest()
        + ".txt"
    )

    with open(filename, "r") as f:
        data = f.readlines()

    data = data[comment_size:]

    total_commits = 0

    for line in data:

        parts = line.split()

        if len(parts) >= 3:
            total_commits += int(parts[2])

    return total_commits


# ============================================================
# API call counter
# ============================================================

def query_count(function_id):
    """
    Counts GitHub GraphQL API calls.
    """

    global QUERY_COUNT

    QUERY_COUNT[function_id] += 1


# ============================================================
# Performance timer
# ============================================================

def perf_counter(funct, *args):
    """
    Measures execution time of a function.
    """

    start = time.perf_counter()

    funct_return = funct(*args)

    return (
        funct_return,
        time.perf_counter() - start
    )


def formatter(
    query_type,
    difference,
    funct_return=False,
    whitespace=0
):
    """
    Prints function execution time.
    """

    print(
        "{:<23}".format(
            "   " + query_type + ":"
        ),
        sep="",
        end=""
    )

    if difference > 1:

        print(
            "{:>12}".format(
                "%.4f" % difference + " s "
            )
        )

    else:

        print(
            "{:>12}".format(
                "%.4f" % (difference * 1000)
                + " ms"
            )
        )

    if whitespace:

        return (
            "{:,}".format(funct_return)
            + " " * max(
                0,
                whitespace - len(
                    "{:,}".format(funct_return)
                )
            )
        )

    return funct_return


# ============================================================
# Main program
# ============================================================

if __name__ == "__main__":

    print("Calculation times:")

    # --------------------------------------------------------
    # Get account information
    # --------------------------------------------------------

    user_data, user_time = perf_counter(
        user_getter,
        USER_NAME
    )

    # IMPORTANT:
    # user_data is a dictionary:
    # {"id": "..."}
    #
    # The original Andrew script incorrectly did:
    #
    # OWNER_ID, acc_date = user_data
    #
    # which makes OWNER_ID equal to the string "id".
    #
    # We correctly extract the actual GitHub ID here.

    OWNER_ID = user_data["id"]

    formatter(
        "account data",
        user_time
    )

    # --------------------------------------------------------
    # Account age
    # --------------------------------------------------------

    age_data, age_time = perf_counter(
        daily_readme,
        user_time_to_iso(
            USER_NAME
        )
    )

    formatter(
        "account age",
        age_time
    )

    # --------------------------------------------------------
    # Lines of code
    # --------------------------------------------------------

    total_loc, loc_time = perf_counter(
        loc_query,
        [
            "OWNER",
            "COLLABORATOR",
            "ORGANIZATION_MEMBER"
        ],
        7
    )

    if total_loc[-1]:

        formatter(
            "LOC (cached)",
            loc_time
        )

    else:

        formatter(
            "LOC (no cache)",
            loc_time
        )

    # --------------------------------------------------------
    # Commits
    # --------------------------------------------------------

    commit_data, commit_time = perf_counter(
        commit_counter,
        7
    )

    formatter(
        "commits",
        commit_time
    )

    # --------------------------------------------------------
    # Stars
    # --------------------------------------------------------

    star_data, star_time = perf_counter(
        graph_repos_stars,
        "stars",
        ["OWNER"]
    )

    formatter(
        "stars",
        star_time
    )

    # --------------------------------------------------------
    # Owned repositories
    # --------------------------------------------------------

    repo_data, repo_time = perf_counter(
        graph_repos_stars,
        "repos",
        ["OWNER"]
    )

    formatter(
        "repositories",
        repo_time
    )

    # --------------------------------------------------------
    # Contributed repositories
    # --------------------------------------------------------

    contrib_data, contrib_time = perf_counter(
        graph_repos_stars,
        "repos",
        [
            "OWNER",
            "COLLABORATOR",
            "ORGANIZATION_MEMBER"
        ]
    )

    formatter(
        "contributed repos",
        contrib_time
    )

    # --------------------------------------------------------
    # Followers
    # --------------------------------------------------------

    follower_data, follower_time = perf_counter(
        follower_getter,
        USER_NAME
    )

    formatter(
        "followers",
        follower_time
    )

    # --------------------------------------------------------
    # Format LOC numbers
    # --------------------------------------------------------

    for index in range(len(total_loc) - 1):

        total_loc[index] = "{:,}".format(
            total_loc[index]
        )

    # --------------------------------------------------------
    # Update dark mode SVG
    # --------------------------------------------------------

    svg_overwrite(
        "dark_mode.svg",
        age_data,
        commit_data,
        star_data,
        repo_data,
        contrib_data,
        follower_data,
        total_loc[:-1]
    )

    # --------------------------------------------------------
    # Print API statistics
    # --------------------------------------------------------

    print(
        "Total GitHub GraphQL API calls:",
        "{:>3}".format(
            sum(QUERY_COUNT.values())
        )
    )

    for function_name, count in QUERY_COUNT.items():

        print(
            "{:<28}".format(
                "   " + function_name + ":"
            ),
            "{:>6}".format(count)
        )


# ============================================================
# Helper for getting account creation date
# ============================================================

def user_time_to_iso(username):
    """
    Gets the GitHub account creation timestamp.

    This is used instead of Andrew's personal birthday.
    """

    query_count("user_getter")

    query = """
    query($login: String!) {

        user(login: $login) {
            createdAt
        }

    }
    """

    request = simple_request(
        user_getter.__name__,
        query,
        {
            "login": username
        }
    )

    return request.json()["data"]["user"]["createdAt"]
