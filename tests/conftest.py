def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: longer integration coverage — runs in the default suite, may be "
        "skipped locally with -m 'not slow'",
    )
