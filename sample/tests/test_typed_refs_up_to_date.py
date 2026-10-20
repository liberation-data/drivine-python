"""The typed references the build writes match the models: a model changed since the build fails here."""

from drivine_sample.stubs import write


def test_the_typed_references_match_the_models():
    assert write(check=True) == 0
