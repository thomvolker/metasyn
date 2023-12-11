"""Module for distribution providers.

These are used to find/fit distributions that are available. See pyproject.toml on how the
builtin distribution provider is registered.
"""

from __future__ import annotations

import inspect
import warnings
from abc import ABC
from typing import Any, List, Optional, Type, Union

try:
    from importlib_metadata import EntryPoint, entry_points
except ImportError:
    from importlib.metadata import EntryPoint, entry_points  # type: ignore

import numpy as np
import polars as pl

from metasyn.distribution import legacy
from metasyn.distribution.base import BaseDistribution
from metasyn.distribution.categorical import MultinoulliDistribution
from metasyn.distribution.continuous import (
    ExponentialDistribution,
    LogNormalDistribution,
    NormalDistribution,
    TruncatedNormalDistribution,
    UniformDistribution,
)
from metasyn.distribution.datetime import (
    UniformDateDistribution,
    UniformDateTimeDistribution,
    UniformTimeDistribution,
)
from metasyn.distribution.discrete import (
    DiscreteNormalDistribution,
    DiscreteTruncatedNormalDistribution,
    DiscreteUniformDistribution,
    PoissonDistribution,
    UniqueKeyDistribution,
)
from metasyn.distribution.faker import (
    FakerDistribution,
    FreeTextDistribution,
    UniqueFakerDistribution,
)
from metasyn.distribution.na import NADistribution
from metasyn.distribution.regex import RegexDistribution, UniqueRegexDistribution
from metasyn.privacy import BasePrivacy, BasicPrivacy


class BaseDistributionProvider(ABC):
    """Class that encapsulates a set of distributions.

    It has a property {var_type}_distributions for every var_type.
    """

    name = ""
    version = ""
    distributions: list[type[BaseDistribution]] = []
    legacy_distributions: list[type[BaseDistribution]] = []

    def __init__(self):
        # Perform internal consistency check.
        assert len(self.name) > 0
        assert len(self.version) > 0
        assert len(self.distributions) > 0

    def get_dist_list(self, var_type: Optional[str],
                      use_legacy: bool = False) -> List[Type[BaseDistribution]]:
        """Get all distributions for a certain variable type.

        Parameters
        ----------
        var_type:
            Variable type to get the distributions for.
        use_legacy:
            Whether to find the distributions in the legacy distribution list.

        Returns
        -------
        list[Type[BaseDistribution]]:
            List of distributions with that variable type.
        """
        dist_list = []
        if use_legacy:
            distributions = self.legacy_distributions
        else:
            distributions = self.distributions
        if var_type is None:
            return distributions
        for dist_class in distributions:
            str_chk = (isinstance(dist_class.var_type, str) and var_type == dist_class.var_type)
            lst_chk = (not isinstance(dist_class.var_type, str) and var_type in dist_class.var_type)
            if str_chk or lst_chk:
                dist_list.append(dist_class)
        return dist_list

    @property
    def all_var_types(self) -> List[str]:
        """Return list of available variable types."""
        var_type_set = set()
        for dist in self.distributions:
            if isinstance(dist.var_type, str):
                var_type_set.add(dist.var_type)
            else:
                var_type_set.update(dist.var_type)
        return list(var_type_set)


class BuiltinDistributionProvider(BaseDistributionProvider):
    """Distribution tree that includes the builtin distributions."""

    name = "builtin"
    version = "1.1"
    distributions = [
        DiscreteNormalDistribution, DiscreteTruncatedNormalDistribution,
        DiscreteUniformDistribution, PoissonDistribution, UniqueKeyDistribution,
        UniformDistribution, NormalDistribution, LogNormalDistribution,
        TruncatedNormalDistribution, ExponentialDistribution,
        MultinoulliDistribution,
        RegexDistribution, UniqueRegexDistribution, FakerDistribution, UniqueFakerDistribution,
        FreeTextDistribution,
        UniformDateDistribution,
        UniformTimeDistribution,
        UniformDateTimeDistribution,
    ]
    legacy_distributions = [
        legacy.RegexDistribution, legacy.UniqueRegexDistribution
    ]


class DistributionProviderList():
    """List of DistributionProviders with functionality to fit distributions.

    Arguments
    ---------
    dist_providers:
        One or more distribution providers, that are denoted either with a string ("builtin")
        , DistributionProvider (BuiltinDistributionProvider()) or DistributionProvider type
        (BuiltinDistributionProvider).
        The order in which distribution providers are included matters. If a provider implements
        the same distribution at the same privacy level, then only the first will be taken into
        account.
    """

    def __init__(
            self,
            dist_providers: Union[
                None, str, type[BaseDistributionProvider], BaseDistributionProvider,
                list[Union[str, type[BaseDistributionProvider], BaseDistributionProvider]]]):
        if dist_providers is None:
            self.dist_packages = _get_all_provider_list()
            return

        if isinstance(dist_providers, (str, type, BaseDistributionProvider)):
            dist_providers = [dist_providers]
        self.dist_packages = []
        for provider in dist_providers:
            if isinstance(provider, str):
                self.dist_packages.append(get_distribution_provider(provider))
            elif isinstance(provider, type):
                self.dist_packages.append(provider())
            elif isinstance(provider, BaseDistributionProvider):
                self.dist_packages.append(provider)
            else:
                raise ValueError(f"Unknown distribution package type '{type(provider)}'")

    def fit(self, series: pl.Series,
            var_type: str,
            dist: Optional[Union[str, BaseDistribution, type]] = None,
            privacy: BasePrivacy = BasicPrivacy(),
            unique: Optional[bool] = None,
            fit_kwargs: Optional[dict] = None):
        """Fit a distribution to a column/series.

        Parameters
        ----------
        series:
            The data to fit the distributions to.
        var_type:
            The variable type of the data.
        dist:
            Distribution to fit. If not supplied or None, the information
            criterion will be used to determine which distribution is the most
            suitable. For most variable types, the information criterion is based on
            the BIC (Bayesian Information Criterion).
        privacy:
            Level of privacy that will be used in the fit.
        unique:
            Whether the distribution should be unique or not.
        fit_kwargs:
            Extra options for distributions during the fitting stage.
        """
        if fit_kwargs is None:
            fit_kwargs = {}
        if dist is not None:
            return self._fit_distribution(series, dist, privacy, **fit_kwargs)
        if len(fit_kwargs) > 0:
            raise ValueError(f"Got fit arguments for variable '{series.name}', but no "
                             "distribution. Set the distribution manually to fix.")
        return self._find_best_fit(series, var_type, unique, privacy)

    def _find_best_fit(self, series: pl.Series, var_type: str,
                       unique: Optional[bool],
                       privacy: BasePrivacy) -> BaseDistribution:
        """Fit a distribution to a series.

        Search for the distribution within all available distributions in the tree.

        Parameters
        ----------
        series:
            Series to fit a distribution to.
        var_type:
            Variable type of the series.
        unique:
            Whether the variable should be unique or not.
        privacy:
            Privacy level to find the best fit with.

        Returns
        -------
        BaseDistribution:
            Distribution fitted to the series.
        """
        if len(series.drop_nulls()) == 0:
            return NADistribution()
        dist_list = self.get_distributions(privacy, var_type)
        if len(dist_list) == 0:
            raise ValueError(f"No available distributions with variable type: '{var_type}'")
        dist_instances = [d.fit(series, **privacy.fit_kwargs) for d in dist_list]
        dist_aic = [d.information_criterion(series) for d in dist_instances]
        i_best_dist = np.argmin(dist_aic)
        warnings.simplefilter("always")
        if dist_instances[i_best_dist].is_unique and unique is None:
            warnings.warn(f"\nVariable {series.name} seems unique, but not set to be unique.\n"
                          "Set the variable to be either unique or not unique to remove this "
                          "warning.\n")
        if unique is None:
            unique = False

        dist_aic = [dist_aic[i] for i in range(len(dist_aic))
                    if dist_instances[i].is_unique == unique]
        dist_instances = [d for d in dist_instances if d.is_unique == unique]
        if len(dist_instances) == 0:
            raise ValueError(f"No available distributions for variable '{series.name}'"
                             f" with variable type '{var_type}' "
                             f"that have unique == {unique}.")
        return dist_instances[np.argmin(dist_aic)]

    def find_distribution(self,  # pylint: disable=too-many-branches
                          dist_name: str,
                          privacy: BasePrivacy = BasicPrivacy(),
                          var_type: Optional[str] = None,
                          version: Optional[str] = None) -> type[BaseDistribution]:
        """Find a distribution and fit keyword arguments from a name.

        Parameters
        ----------
        dist_name:
            Name of the distribution, e.g., for the built-in
            uniform distribution: "uniform", "core.uniform", "UniformDistribution".
        privacy:
            Type of privacy to be applied.
        var_type:
            Type of the variable to find. If var_type is None, then do not check the
            variable type.
        version:
            Version of the distribution to get. If necessary get them from legacy.

        Returns
        -------
        tuple[Type[BaseDistribution], dict[str, Any]]:
            A distribution and the arguments to create an instance.
        """
        if NADistribution.matches_name(dist_name):
            return NADistribution

        versions_found = []
        for dist_class in self.get_distributions(privacy, var_type=var_type) + [NADistribution]:
            if dist_class.matches_name(dist_name):
                if version is None or version == dist_class.version:
                    return dist_class
                versions_found.append(dist_class)

        # Look for distribution in legacy
        warnings.simplefilter("always")
        legacy_distribs = [
            dist_class
            for dist_class in self.get_distributions(privacy, use_legacy=True, var_type=var_type)
            if dist_class.matches_name(dist_name)]

        if len(legacy_distribs+versions_found) == 0:
            raise ValueError(f"Cannot find distribution with name '{dist_name}'.")

        if len(versions_found) == 0:
            warnings.warn("Distribution with name '{dist_name}' is deprecated and "
                          "will be removed in the future.")

        # Find exact matches in legacy distributions
        legacy_versions = [dist.version for dist in legacy_distribs]
        if version is not None and version in legacy_versions:
            warnings.warn("Version ({version}) of distribution with name '{dist_name}'"
                          " is deprecated and will be removed in the future.")
            return legacy_distribs[legacy_versions.index(version)]

        # If version is None, take the latest version.
        if version is None:
            scores = [int(dist.version.split(".")[0])*100 + int(dist.version.split(".")[1])
                      for dist in legacy_distribs]
            i_min = np.argmax(scores)
            return legacy_distribs[i_min]

        # Find the distribution with the same major revision, and closest minor revision.
        major_version = int(version.split(".")[0])
        minor_version = int(version.split(".")[1])
        all_dist = versions_found + legacy_distribs
        all_versions = [[int(x) for x in dist.version.split(".")] for dist in all_dist]
        score = [int(ver[0] == major_version)*1000000 - (ver[1]-minor_version)**2
                 for ver in all_versions]
        i_max = np.argmax(score)

        # Wrong major revision.
        if score[i_max] < 500000:
            raise ValueError(
                f"Cannot find compatible version for distribution '{dist_name}', available: "
                f"{legacy_versions+versions_found}")

        warnings.warn("Version mismatch ({version}) versus ({all_dist[i_max].version}))")
        return all_dist[i_max]

    def _fit_distribution(self, series: pl.Series,
                          dist: Union[str, Type[BaseDistribution], BaseDistribution],
                          privacy: BasePrivacy,
                          **fit_kwargs) -> BaseDistribution:
        """Fit a specific distribution to a series.

        In contrast the fit method, this needs a supplied distribution(type).

        Parameters
        ----------
        dist:
            Distribution to fit (if it is not already fitted).
        series:
            Series to fit the distribution to.
        privacy:
            Privacy level to fit the distribution with.
        fit_kwargs:
            Extra keyword arguments to modify the way the distribution is fit.

        Returns
        -------
        BaseDistribution:
            Fitted distribution.
        """
        dist_instance = None
        if isinstance(dist, BaseDistribution):
            return dist

        if isinstance(dist, str):
            dist_class = self.find_distribution(dist, privacy=privacy)
        elif inspect.isclass(dist) and issubclass(dist, BaseDistribution):
            dist_class = dist
        else:
            raise TypeError(
                f"Distribution {dist} with type {type(dist)} is not a BaseDistribution")
        if issubclass(dist_class, NADistribution):
            dist_instance = dist_class.default_distribution()
        else:
            dist_instance = dist_class.fit(series, **privacy.fit_kwargs, **fit_kwargs)
        return dist_instance

    def get_distributions(self, privacy: Optional[BasePrivacy] = None,
                          var_type: Optional[str] = None,
                          use_legacy: bool = False) -> list[type[BaseDistribution]]:
        """Get the available distributions with constraints.

        Parameters
        ----------
        privacy:
            Privacy level/type to filter the distributions.
        var_type:
            Variable type to filter for, e.g. 'string'.
        use_legacy:
            Whether to use legacy distributions or not.

        Returns
        -------
        dist_list:
            List of distributions that fit the given constraints.
        """
        dist_list = []
        for dist_provider in self.dist_packages:
            dist_list.extend(dist_provider.get_dist_list(var_type, use_legacy=use_legacy))

        if privacy is None:
            return dist_list
        dist_list = [dist for dist in dist_list if dist.privacy == privacy.name]
        return dist_list

    def from_dict(self, var_dict: dict[str, Any]) -> BaseDistribution:
        """Create a distribution from a dictionary.

        Parameters
        ----------
        var_dict:
            Variable dictionary that includes the distribution properties.

        Returns
        -------
        BaseDistribution:
            Distribution representing the dictionary.
        """
        dist_name = var_dict["distribution"]["implements"]
        version = var_dict["distribution"].get("version", "1.0")
        var_type = var_dict["type"]
        dist_class = self.find_distribution(dist_name, version=version, var_type=var_type)
        return dist_class.from_dict(var_dict["distribution"])


def _get_all_providers() -> dict[str, EntryPoint]:
    """Get all available providers."""
    return {
        entry.name: entry
        for entry in entry_points(group="metasyn.distribution_provider")
    }


def _get_all_provider_list() -> list[BaseDistributionProvider]:
    return [p.load()() for p in _get_all_providers().values()]


def get_distribution_provider(
        provider: Union[str, type[BaseDistributionProvider],
                        BaseDistributionProvider] = "builtin"
        ) -> BaseDistributionProvider:
    """Get a distribution tree.

    Parameters
    ----------
    provider:
        Name, class or class type of the provider to be used.
    kwargs:
        Extra keyword arguments for initialization of the distribution provider.

    Returns
    -------
    BaseDistributionProvider:
        The distribution provider that was found.
    """
    if isinstance(provider, BaseDistributionProvider):
        return provider
    if isinstance(provider, type):
        return provider()

    all_providers = _get_all_providers()
    try:
        return all_providers[provider].load()()
    except KeyError as exc:
        raise ValueError(f"Cannot find distribution provider with name '{provider}'.") from exc