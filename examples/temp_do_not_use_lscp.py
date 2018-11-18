# system imports
import os
import collections
import sys
import datetime
import warnings

# numpy
import numpy as np

# sklearn imports
from sklearn.utils.estimator_checks import check_estimator
from sklearn.neighbors import KDTree
from sklearn.utils import check_array
from sklearn.utils import check_random_state
from sklearn.utils.validation import check_is_fitted
from sklearn.utils.random import sample_without_replacement

# PYOD imports
sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname("__file__"), '..')))

from pyod.models.base import BaseDetector
from pyod.utils.stat_models import pearsonr
from pyod.utils.utility import argmaxn
from pyod.utils.utility import precision_n_scores, standardizer


def argmaxp(a, p):
    """Utility function to return the index of top p values in a
    :param a: list variable
    :param p: number of elements to select
    :return: index of top p elements in a
    """

    a = np.asarray(a).ravel()
    length = a.shape[0]
    pth = np.argpartition(a, length - p)
    return pth[length - p:]


def generate_bagging_indices(random_state, bootstrap_features, n_features,
                             min_features, max_features):
    """
    Randomly draw feature indices. Internal use only.

    Modified from sklearn/ensemble/bagging.py
    """
    # Get valid random state
    random_state = check_random_state(random_state)

    # decide number of features to draw
    random_n_features = random_state.randint(min_features, max_features)

    # Draw indices
    feature_indices = _generate_indices(random_state, bootstrap_features,
                                        n_features, random_n_features)

    return feature_indices


def _generate_indices(random_state, bootstrap, n_population, n_samples):
    """
    Draw randomly sampled indices. Internal use only.

    See sklearn/ensemble/bagging.py
    """
    # Draw sample indices
    if bootstrap:
        indices = random_state.randint(0, n_population, n_samples)
    else:
        indices = sample_without_replacement(n_population, n_samples,
                                             random_state=random_state)

    return indices


# access the timestamp for logging purpose
today = datetime.datetime.now()
timestamp = today.strftime("%Y%m%d_%H%M%S")

# set numpy parameters
np.set_printoptions(suppress=True, precision=4)


class LSCP(BaseDetector):

    def __init__(self, estimator_list, n_iterations=20, local_region_size=30, local_max_features=1.0, n_bins=10, random_state=42):
        self.estimator_list = estimator_list
        self.n_clf = len(self.estimator_list)
        self.n_iterations = n_iterations
        self.local_region_size = local_region_size
        self.local_region_min = 30
        self.local_region_max = 100
        self.local_max_features = local_max_features
        self.local_min_features = 0.5
        self.local_region_iterations = 20
        self.local_region_threshold = int(self.local_region_iterations / 2)
        self.n_bins = n_bins
        self.n_selected = 1
        self.random_state = random_state

        assert len(estimator_list) > 1, "The estimator list has less than 2 estimators."

        if self.n_bins >= self.n_clf:
            warnings.warn("Number of histogram bins greater than number of classifiers, reducing n_bins to n_clf.")
            self.n_bins = self.n_clf

        for estimator in self.estimator_list:
            check_estimator(estimator)

    # TODO: discuss how to standardize data for different model types
    def fit(self, X, y=None):

        X = check_array(X)
        self.n_features_ = X.shape[1]

        # normalize input data
        self.X_train_norm_ = standardizer(X)
        train_scores = np.zeros([self.X_train_norm_.shape[0], self.n_clf])

        # fit each base estimator and calculate standardized train scores
        for k, estimator in enumerate(self.estimator_list):
            estimator.fit(self.X_train_norm_, y)
            train_scores[:, k] = estimator.predict(self.X_train_norm_)
        self.train_scores_norm_ = standardizer(train_scores)

        # generate pseudo target for training --> for calculating weights
        self.training_pseudo_label_ = np.max(self.train_scores_norm_, axis=1).reshape(-1, 1)
        self.decision_scores_ = True
        self.threshold_ = True
        self.labels_ = True

        return

    def decision_function(self, X):
        # check whether fmodel has been fit
        check_is_fitted(self, ['training_pseudo_label_', 'train_scores_norm_', 'X_train_norm_', 'n_features_'])

        # check input array
        X = check_array(X)
        if self.n_features_ != X.shape[1]:
            raise ValueError("Number of features of the model must "
                             "match the input. Model n_features is {0} and "
                             "input n_features is {1}."
                             "".format(self.n_features_, X.shape[1]))

        # ensure local region size is within acceptable limits
        self.local_region_size = min(self.local_region_size, self.local_region_min)
        self.local_region_size = max(self.local_region_size, self.local_region_max)

        # standardize test data and get local region for each test instance
        X_test_norm = standardizer(X)
        ind_arr = self._get_local_region(X_test_norm)

        # calculate test scores
        test_scores = np.zeros([X_test_norm.shape[0], self.n_clf])
        for k, estimator in enumerate(self.estimator_list):
            test_scores[:, k] = estimator.predict(X_test_norm)
        test_scores_norm = standardizer(test_scores)

        # placeholder for predictions
        pred_scores_ens = np.zeros([X_test_norm.shape[0], ])

        # iterate through test instances (ind_arr indices correspond to x_test)
        for i, ind_k in enumerate(ind_arr):

            # get pseudo target and training scores in local region of test instance
            local_pseudo_ground_truth = self.training_pseudo_label_[ind_k,].ravel()
            local_train_scores = self.train_scores_norm_[ind_k, :]

            # calculate pearson correlation between local pseudo ground truth and local train scores
            pearson_corr_scores = np.zeros([self.n_clf, ])
            for d in range(self.n_clf):
                pearson_corr_scores[d, ] = pearsonr(local_pseudo_ground_truth, local_train_scores[:, d])[0]

            # return best score
            pred_scores_ens[i,] = np.mean(
                test_scores_norm[i, self._get_competent_detectors(pearson_corr_scores)])

        return pred_scores_ens


    def _get_local_region(self, X_test_norm):

        # Initialize the local region list
        grid = [[]] * X_test_norm.shape[0]

        for t in range(self.local_region_iterations):
            features = generate_bagging_indices(self.random_state,
                                                bootstrap_features=False,
                                                n_features=self.X_train_norm_.shape[1],
                                                min_features=int(
                                                    self.X_train_norm_.shape[
                                                        1] * self.local_min_features),
                                                max_features=self.X_train_norm_.shape[1])

            tree = KDTree(self.X_train_norm_[:, features])
            dist_arr, ind_arr = tree.query(X_test_norm[:, features],
                                           k=self.local_region_size)

            for j in range(X_test_norm.shape[0]):
                grid[j] = grid[j] + ind_arr[j, :].tolist()

        grid_f = [[]] * X_test_norm.shape[0]
        for j in range(X_test_norm.shape[0]):
            grid_f[j] = [item for item, count in
                         collections.Counter(grid[j]).items() if
                         count > self.local_region_threshold]

        return grid_f

    def _get_competent_detectors(self, scores):
        """ algorithm for selecting the most competent detectors
        :param scores:
        :param n_bins:
        :param n_selected:
        :return:
        """
        scores = scores.reshape(-1, 1)
        hist, bin_edges = np.histogram(scores, bins=self.n_bins)
        #    dense_bin = np.argmax(hist)
        max_bins = argmaxn(hist, n=self.n_selected)
        candidates = []
        #    print(hist)
        for max_bin in max_bins:
            #        print(bin_edges[max_bin], bin_edges[max_bin+1])
            selected = np.where((scores >= bin_edges[max_bin])
                                & (scores <= bin_edges[max_bin + 1]))
            #        print(selected)
            candidates = candidates + selected[0].tolist()

        #    print(np.mean(scores[candidates,:]), np.mean(scores))
        # return np.mean(scores[candidates, :])
        return candidates

    def _get_decision_scores(self):
        pass

    def __len__(self):
        return len(self.estimator_list)

    def __getitem__(self, index):
        return self.estimator_list[index]

    def __iter__(self):
        return iter(self.estimator_list)

