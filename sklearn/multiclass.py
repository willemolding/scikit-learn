"""
Multiclass and multilabel classification strategies
===================================================

This module implements multiclass learning algorithms:
    - one-vs-the-rest / one-vs-all
    - one-vs-one
    - error correcting output codes

The estimators provided in this module are meta-estimators: they require a base
estimator to be provided in their constructor. For example, it is possible to
use these estimators to turn a binary classifier or a regressor into a
multiclass classifier. It is also possible to use these estimators with
multiclass estimators in the hope that their accuracy or runtime performance
improves.

All classifiers in scikit-learn implement multiclass classification; you
only need to use this module if you want to experiment with custom multiclass
strategies.

The one-vs-the-rest meta-classifier also implements a `predict_proba` method,
so long as such a method is implemented by the base classifier. This method
returns probabilities of class membership in both the single label and
multilabel case.  Note that in the multilabel case, probabilities are the
marginal probability that a given sample falls in the given class. As such, in
the multilabel case the sum of these probabilities over all possible labels
for a given sample *will not* sum to unity, as they do in the single label
case.
"""

# Author: Mathieu Blondel <mathieu@mblondel.org>
# Author: Hamzeh Alsalhi <93hamsal@gmail.com>
#
# License: BSD 3 clause

import array
import numpy as np
import warnings
import scipy.sparse as sp

from .base import BaseEstimator, ClassifierMixin, clone, is_classifier
from .base import MetaEstimatorMixin, is_regressor
from .preprocessing import LabelBinarizer
from .metrics.pairwise import pairwise_distances_argmin
from .utils import check_random_state
from .utils.validation import _num_samples
from .utils.validation import check_consistent_length
from .utils.validation import check_is_fitted
from .utils.validation import check_array
from .externals.joblib import Parallel
from .externals.joblib import delayed

__all__ = [
    "OneVsRestClassifier",
    "OneVsOneClassifier",
    "OutputCodeClassifier",
]


def _fit_binary(estimator, X, y, classes=None, ternary=False):
    """Fit a single binary estimator."""
    if ternary:
        X = X[y != 0]
        y = y[y != 0]

    unique_y = np.unique(y)
    if len(unique_y) == 1: #column contains all the same values
        if classes is not None:
            if y[0] == -1:
                c = 0
            else:
                c = y[0]
            warnings.warn("Label %s is present in all training examples." %
                          str(classes[c]))
        estimator = _ConstantPredictor().fit(X, unique_y)
    else:
        estimator = clone(estimator)
        estimator.fit(X, y)

    return estimator


def _predict_binary(estimator, X):
    """Make predictions using a single binary estimator."""
    if is_regressor(estimator):
        return estimator.predict(X)
    try:
        score = np.ravel(estimator.decision_function(X))
    except (AttributeError, NotImplementedError):
        # probabilities of the positive class
        score = estimator.predict_proba(X)[:, 1]
    return score


def _check_estimator(estimator):
    """Make sure that an estimator implements the necessary methods."""
    if (not hasattr(estimator, "decision_function") and
            not hasattr(estimator, "predict_proba")):
        raise ValueError("The base estimator should implement "
                         "decision_function or predict_proba!")

def _check_codebook(codebook, n_classes):
    """Checks a codebook matrix to ensure it meets the requirements"""
    codebook = check_array(codebook)
    if codebook.shape[0] != n_classes:
        raise ValueError(
            "The codebook must have %d rows but %d rows were detected"
            %(n_classes, codebook.shape[0]))
    return codebook




class _ConstantPredictor(BaseEstimator):

    def fit(self, X, y):
        self.y_ = y
        return self

    def predict(self, X):
        check_is_fitted(self, 'y_')

        return np.repeat(self.y_, X.shape[0])

    def decision_function(self, X):
        check_is_fitted(self, 'y_')

        return np.repeat(self.y_, X.shape[0])

    def predict_proba(self, X):
        check_is_fitted(self, 'y_')

        return np.repeat([np.hstack([1 - self.y_, self.y_])],
                         X.shape[0], axis=0)


class OneVsRestClassifier(BaseEstimator, ClassifierMixin, MetaEstimatorMixin):
    """One-vs-the-rest (OvR) multiclass/multilabel strategy

    Also known as one-vs-all, this strategy consists in fitting one classifier
    per class. For each classifier, the class is fitted against all the other
    classes. In addition to its computational efficiency (only `n_classes`
    classifiers are needed), one advantage of this approach is its
    interpretability. Since each class is represented by one and one classifier
    only, it is possible to gain knowledge about the class by inspecting its
    corresponding classifier. This is the most commonly used strategy for
    multiclass classification and is a fair default choice.

    This strategy can also be used for multilabel learning, where a classifier
    is used to predict multiple labels for instance, by fitting on a 2-d matrix
    in which cell [i, j] is 1 if sample i has label j and 0 otherwise.

    In the multilabel learning literature, OvR is also known as the binary
    relevance method.

    Read more in the :ref:`User Guide <ovr_classification>`.

    Parameters
    ----------
    estimator : estimator object
        An estimator object implementing `fit` and one of `decision_function`
        or `predict_proba`.

    n_jobs : int, optional, default: 1
        The number of jobs to use for the computation. If -1 all CPUs are used.
        If 1 is given, no parallel computing code is used at all, which is
        useful for debugging. For n_jobs below -1, (n_cpus + 1 + n_jobs) are
        used. Thus for n_jobs = -2, all CPUs but one are used.

    Attributes
    ----------
    estimators_ : list of `n_classes` estimators
        Estimators used for predictions.

    classes_ : array, shape = [`n_classes`]
        Class labels.
    label_binarizer_ : LabelBinarizer object
        Object used to transform multiclass labels to binary labels and
        vice-versa.
    multilabel_ : boolean
        Whether a OneVsRestClassifier is a multilabel classifier.
    """

    def __init__(self, estimator, n_jobs=1):
        self.estimator = estimator
        self.n_jobs = n_jobs

    def fit(self, X, y):
        """Fit underlying estimators.

        Parameters
        ----------
        X : (sparse) array-like, shape = [n_samples, n_features]
            Data.

        y : (sparse) array-like, shape = [n_samples] or [n_samples, n_classes]
            Multi-class targets. An indicator matrix turns on multilabel
            classification.

        Returns
        -------
        self
        """
        # A sparse LabelBinarizer, with sparse_output=True, has been shown to
        # outpreform or match a dense label binarizer in all cases and has also
        # resulted in less or equal memory consumption in the fit_ovr function
        # overall.
        self.label_binarizer_ = LabelBinarizer(sparse_output=True)
        Y = self.label_binarizer_.fit_transform(y)
        Y = Y.tocsc()
        columns = (col.toarray().ravel() for col in Y.T)
        # In cases where individual estimators are very fast to train setting
        # n_jobs > 1 in can results in slower performance due to the overhead
        # of spawning threads.  See joblib issue #112.
        self.estimators_ = Parallel(n_jobs=self.n_jobs)(delayed(_fit_binary)(
            self.estimator, X, column, classes=[
                "not %s" % self.label_binarizer_.classes_[i],
                self.label_binarizer_.classes_[i]])
            for i, column in enumerate(columns))

        return self

    def predict(self, X):
        """Predict multi-class targets using underlying estimators.

        Parameters
        ----------
        X : (sparse) array-like, shape = [n_samples, n_features]
            Data.

        Returns
        -------
        y : (sparse) array-like, shape = [n_samples] or [n_samples, n_classes].
            Predicted multi-class targets.
        """
        check_is_fitted(self, 'estimators_')
        if (hasattr(self.estimators_[0], "decision_function") and
                is_classifier(self.estimators_[0])):
            thresh = 0
        else:
            thresh = .5

        n_samples = _num_samples(X)
        if self.label_binarizer_.y_type_ == "multiclass":
            maxima = np.empty(n_samples, dtype=float)
            maxima.fill(-np.inf)
            argmaxima = np.zeros(n_samples, dtype=int)
            for i, e in enumerate(self.estimators_):
                pred = _predict_binary(e, X)
                np.maximum(maxima, pred, out=maxima)
                argmaxima[maxima == pred] = i
            return self.label_binarizer_.classes_[np.array(argmaxima.T)]
        else:
            indices = array.array('i')
            indptr = array.array('i', [0])
            for e in self.estimators_:
                indices.extend(np.where(_predict_binary(e, X) > thresh)[0])
                indptr.append(len(indices))
            data = np.ones(len(indices), dtype=int)
            indicator = sp.csc_matrix((data, indices, indptr),
                                      shape=(n_samples, len(self.estimators_)))
            return self.label_binarizer_.inverse_transform(indicator)

    def predict_proba(self, X):
        """Probability estimates.

        The returned estimates for all classes are ordered by label of classes.

        Note that in the multilabel case, each sample can have any number of
        labels. This returns the marginal probability that the given sample has
        the label in question. For example, it is entirely consistent that two
        labels both have a 90% probability of applying to a given sample.

        In the single label multiclass case, the rows of the returned matrix
        sum to 1.

        Parameters
        ----------
        X : array-like, shape = [n_samples, n_features]

        Returns
        -------
        T : (sparse) array-like, shape = [n_samples, n_classes]
            Returns the probability of the sample for each class in the model,
            where classes are ordered as they are in `self.classes_`.
        """
        check_is_fitted(self, 'estimators_')
        # Y[i,j] gives the probability that sample i has the label j.
        # In the multi-label case, these are not disjoint.
        Y = np.array([e.predict_proba(X)[:, 1] for e in self.estimators_]).T

        if len(self.estimators_) == 1:
            # Only one estimator, but we still want to return probabilities
            # for two classes.
            Y = np.concatenate(((1 - Y), Y), axis=1)

        if not self.multilabel_:
            # Then, probabilities should be normalized to 1.
            Y /= np.sum(Y, axis=1)[:, np.newaxis]
        return Y

    def decision_function(self, X):
        """Returns the distance of each sample from the decision boundary for
        each class. This can only be used with estimators which implement the
        decision_function method.

        Parameters
        ----------
        X : array-like, shape = [n_samples, n_features]

        Returns
        -------
        T : array-like, shape = [n_samples, n_classes]
        """
        check_is_fitted(self, 'estimators_')
        if not hasattr(self.estimators_[0], "decision_function"):
            raise AttributeError(
                "Base estimator doesn't have a decision_function attribute.")
        return np.array([est.decision_function(X).ravel()
                         for est in self.estimators_]).T

    @property
    def multilabel_(self):
        """Whether this is a multilabel classifier"""
        return self.label_binarizer_.y_type_.startswith('multilabel')

    @property
    def classes_(self):
        return self.label_binarizer_.classes_

    @property
    def coef_(self):
        check_is_fitted(self, 'estimators_')
        if not hasattr(self.estimators_[0], "coef_"):
            raise AttributeError(
                "Base estimator doesn't have a coef_ attribute.")
        coefs = [e.coef_ for e in self.estimators_]
        if sp.issparse(coefs[0]):
            return sp.vstack(coefs)
        return np.vstack(coefs)

    @property
    def intercept_(self):
        check_is_fitted(self, 'estimators_')
        if not hasattr(self.estimators_[0], "intercept_"):
            raise AttributeError(
                "Base estimator doesn't have an intercept_ attribute.")
        return np.array([e.intercept_.ravel() for e in self.estimators_])


def _fit_ovo_binary(estimator, X, y, i, j):
    """Fit a single binary estimator (one-vs-one)."""
    cond = np.logical_or(y == i, y == j)
    y = y[cond]
    y_binary = np.empty(y.shape, np.int)
    y_binary[y == i] = 0
    y_binary[y == j] = 1
    ind = np.arange(X.shape[0])
    return _fit_binary(estimator, X[ind[cond]], y_binary, classes=[i, j])


class OneVsOneClassifier(BaseEstimator, ClassifierMixin, MetaEstimatorMixin):
    """One-vs-one multiclass strategy

    This strategy consists in fitting one classifier per class pair.
    At prediction time, the class which received the most votes is selected.
    Since it requires to fit `n_classes * (n_classes - 1) / 2` classifiers,
    this method is usually slower than one-vs-the-rest, due to its
    O(n_classes^2) complexity. However, this method may be advantageous for
    algorithms such as kernel algorithms which don't scale well with
    `n_samples`. This is because each individual learning problem only involves
    a small subset of the data whereas, with one-vs-the-rest, the complete
    dataset is used `n_classes` times.

    Read more in the :ref:`User Guide <ovo_classification>`.

    Parameters
    ----------
    estimator : estimator object
        An estimator object implementing `fit` and one of `decision_function`
        or `predict_proba`.

    n_jobs : int, optional, default: 1
        The number of jobs to use for the computation. If -1 all CPUs are used.
        If 1 is given, no parallel computing code is used at all, which is
        useful for debugging. For n_jobs below -1, (n_cpus + 1 + n_jobs) are
        used. Thus for n_jobs = -2, all CPUs but one are used.

    Attributes
    ----------
    estimators_ : list of `n_classes * (n_classes - 1) / 2` estimators
        Estimators used for predictions.

    classes_ : numpy array of shape [n_classes]
        Array containing labels.
    """

    def __init__(self, estimator, n_jobs=1):
        self.estimator = estimator
        self.n_jobs = n_jobs

    def fit(self, X, y):
        """Fit underlying estimators.

        Parameters
        ----------
        X : (sparse) array-like, shape = [n_samples, n_features]
            Data.

        y : array-like, shape = [n_samples]
            Multi-class targets.

        Returns
        -------
        self
        """
        y = np.asarray(y)
        check_consistent_length(X, y)

        self.classes_ = np.unique(y)
        n_classes = self.classes_.shape[0]
        self.estimators_ = Parallel(n_jobs=self.n_jobs)(
            delayed(_fit_ovo_binary)(
                self.estimator, X, y, self.classes_[i], self.classes_[j])
            for i in range(n_classes) for j in range(i + 1, n_classes))

        return self

    def predict(self, X):
        """Estimate the best class label for each sample in X.

        This is implemented as ``argmax(decision_function(X), axis=1)`` which
        will return the label of the class with most votes by estimators
        predicting the outcome of a decision for each possible class pair.

        Parameters
        ----------
        X : (sparse) array-like, shape = [n_samples, n_features]
            Data.

        Returns
        -------
        y : numpy array of shape [n_samples]
            Predicted multi-class targets.
        """
        Y = self.decision_function(X)
        return self.classes_[Y.argmax(axis=1)]

    def decision_function(self, X):
        """Decision function for the OneVsOneClassifier.

        The decision values for the samples are computed by adding the
        normalized sum of pair-wise classification confidence levels to the
        votes in order to disambiguate between the decision values when the
        votes for all the classes are equal leading to a tie.

        Parameters
        ----------
        X : array-like, shape = [n_samples, n_features]

        Returns
        -------
        Y : array-like, shape = [n_samples, n_classes]
        """
        check_is_fitted(self, 'estimators_')

        predictions = np.vstack([est.predict(X) for est in self.estimators_]).T
        confidences = np.vstack([_predict_binary(est, X) for est in self.estimators_]).T
        return _ovr_decision_function(predictions, confidences,
                                      len(self.classes_))


def _ovr_decision_function(predictions, confidences, n_classes):
    """Compute a continuous, tie-breaking ovr decision function.

    It is important to include a continuous value, not only votes,
    to make computing AUC or calibration meaningful.

    Parameters
    ----------
    predictions : array-like, shape (n_samples, n_classifiers)
        Predicted classes for each binary classifier.

    confidences : array-like, shape (n_samples, n_classifiers)
        Decision functions or predicted probabilities for positive class
        for each binary classifier.

    n_classes : int
        Number of classes. n_classifiers must be
        ``n_classes * (n_classes - 1 ) / 2``
    """
    n_samples = predictions.shape[0]
    votes = np.zeros((n_samples, n_classes))
    sum_of_confidences = np.zeros((n_samples, n_classes))

    k = 0
    for i in range(n_classes):
        for j in range(i + 1, n_classes):
            sum_of_confidences[:, i] -= confidences[:, k]
            sum_of_confidences[:, j] += confidences[:, k]
            votes[predictions[:, k] == 0, i] += 1
            votes[predictions[:, k] == 1, j] += 1
            k += 1

    max_confidences = sum_of_confidences.max()
    min_confidences = sum_of_confidences.min()

    if max_confidences == min_confidences:
        return votes

    # Scale the sum_of_confidences to (-0.5, 0.5) and add it with votes.
    # The motivation is to use confidence levels as a way to break ties in
    # the votes without switching any decision made based on a difference
    # of 1 vote.
    eps = np.finfo(sum_of_confidences.dtype).eps
    max_abs_confidence = max(abs(max_confidences), abs(min_confidences))
    scale = (0.5 - eps) / max_abs_confidence
    return votes + sum_of_confidences * scale


def _make_complete_code_book(n_classes, ternary=False):
    """Create a complete code matrix, e.g. all possible (non-trivial) partitions of the classes.

    For a binary code with k classes the matrix will have 2^(k-1) - 1 columns. 

    For a ternary code with k classes the matrix will have (3^k - 2^(k+1) + 1) / 2 columns. 

    This method is not appropriate for a large number of classes."""
    if ternary is False:
        n = 2**(n_classes - 1) - 1
        M = np.zeros((n_classes,n))
        M[1:,:] = np.fromfunction(lambda i, j: (j+1) // (2**(i)) % 2, (n_classes - 1, n))
        M = M*2 - 1
    else:
        # create a matrix of all non-trivial partitions including inversions
        P = np.fromfunction(lambda i, j: (j+1) // (2**(i)) % 2, (n_classes, 2**(n_classes) - 2))

        # For each pair of partitions that are non-overlapping assign half to -1, half to +1 and the rest to 0
        M = []
        for j1 in range(P.shape[1]):
            for j2 in range(j1+1, P.shape[1]):
                if all(P[:,j1] + P[:,j2] < 2): #if the sets are non-overlapping
                    M.append( P[:,j1] + -P[:,j2])
        M = np.array(M).T
    return M

def _make_random_code_book(n_classes, code_size, ternary=False, random_state=None):
    """Randomly generate a binary code given the number of classes"""
    if ternary is False:
        M = random_state.random_integers(0, 1, size=(n_classes, code_size))*2 - 1
    else:
        M = random_state.random_integers(0, 2, size=(n_classes, code_size)) - 1
    return M




def hamming_loss(x, y): return (1. - np.sign(x*y))/2.
def soft_hamming(x, y): return (1. - x*y)/2.
def log_loss(x, y): return np.log(1. + np.exp(-2.*x*y))
def exp_loss(x, y): return np.exp(-x*y)
def logistic_loss(x, y) : return 1./(1. + np.exp(2*x*y))


LOSS_FUNCTIONS = {
    'hamming' : hamming_loss,
    'soft_hamming' : soft_hamming,
    'log' : log_loss,
    'exp' : exp_loss,
    'logistic' : logistic_loss}   


class OutputCodeClassifier(BaseEstimator, ClassifierMixin, MetaEstimatorMixin):
    """(Error-Correcting) Output-Code multiclass strategy

    Output-code based strategies consist in representing each class with a
    binary code (an array of 0s and 1s). At fitting time, one binary
    classifier per bit in the code book is fitted.  At prediction time, the
    classifiers are used to project new points in the class space and the class
    closest to the points is chosen. The main advantage of these strategies is
    that the number of classifiers used can be controlled by the user, either
    for compressing the model (0 < code_size < 1) or for making the model more
    robust to errors (code_size > 1). 

    This also supports the use of ternary codes as described in [4]. Each codeword 
    comprised of either [-1, 0, +1] representing positive and negetive classification and
    also 0 which implies that the particular base class is not considered in the comparison.
    This representation has advantages in that it unifies Output-code and One-vs-One methods.
    It also allows for a greater number of possible comparisons.

    See the documentation for more details.

    Read more in the :ref:`User Guide <ecoc>`.

    Parameters
    ----------
    estimator : estimator object
        An estimator object implementing `fit` and one of `decision_function`
        or `predict_proba`.

    code : string or array-like, (default='random')
        If a string, this must be one of 'random' or 'complete'
        If an array-like must be a binary code matrix of shape (n, n_classes)

    code_size : float
        Percentage of the number of classes to be used to create the code book.
        A number between 0 and 1 will require fewer classifiers than
        one-vs-the-rest. A number greater than 1 will require more classifiers
        than one-vs-the-rest.
        Ignored for ``code=array-like and code='complete'``

    ternary : boolean, default False
        Whether to use ternery codes when calculating the random or complete code

    random_state : numpy.RandomState, optional
        The generator used to initialize the codebook. Defaults to
        numpy.random.

    n_jobs : int, optional, default: 1
        The number of jobs to use for the computation. If -1 all CPUs are used.
        If 1 is given, no parallel computing code is used at all, which is
        useful for debugging. For n_jobs below -1, (n_cpus + 1 + n_jobs) are
        used. Thus for n_jobs = -2, all CPUs but one are used.

    Attributes
    ----------
    estimators_ : list of `int(n_classes * code_size)` estimators
        Estimators used for predictions.

    classes_ : numpy array of shape [n_classes]
        Array containing labels.

    code_book_ : numpy array of shape [n_classes, code_size]
        Binary array containing the code of each class.

    References
    ----------

    .. [1] "Solving multiclass learning problems via error-correcting output
       codes",
       Dietterich T., Bakiri G.,
       Journal of Artificial Intelligence Research 2,
       1995.

    .. [2] "The error coding method and PICTs",
       James G., Hastie T.,
       Journal of Computational and Graphical statistics 7,
       1998.

    .. [3] "The Elements of Statistical Learning",
       Hastie T., Tibshirani R., Friedman J., page 606 (second-edition)
       2008.

    .. [4] "Reducing multiclass to binary: A unifying approach for margin classifiers",
       Allwein, E. L.; Schapire, R. E. & Singer, Y.,
       The Journal of Machine Learning Research, JMLR. org, 2001, 1, 113-141,
       2001.
    """

    def __init__(self, estimator,  code='random', ternary=False, code_size=1.5, random_state=None, n_jobs=1):
        self.estimator = estimator
        self.code = code
        self.code_size = code_size
        self.ternary = ternary
        self.random_state = random_state
        self.n_jobs = n_jobs

    def fit(self, X, y):
        """Fit underlying estimators.

        Parameters
        ----------
        X : (sparse) array-like, shape = [n_samples, n_features]
            Data.

        y : numpy array of shape [n_samples]
            Multi-class targets.

        Returns
        -------
        self
        """
        _check_estimator(self.estimator)
        random_state = check_random_state(self.random_state)

        self.classes_ = np.unique(y)
        n_classes = self.classes_.shape[0]

        if self.code is 'random':
            if self.code_size <= 0:
                raise ValueError("code_size should be greater than 0, got %d"
                                 ""% self.code_size)
            code_size_ = int(n_classes * self.code_size)
            self.code_book_ = _make_random_code_book(n_classes, code_size_, self.ternary, random_state)
        elif self.code is 'complete':
            self.code_book_ = _make_complete_code_book(n_classes, self.ternary)
        else:
            self.code_book_ = _check_codebook(self.code, n_classes)
            if len(np.unique(self.code_book_)) >= 3:
                self.ternary = True

        classes_index = dict((c, i) for i, c in enumerate(self.classes_))

        Y = np.array([self.code_book_[classes_index[y[i]]]
                      for i in range(X.shape[0])], dtype=np.int)

        self.estimators_ = Parallel(n_jobs=self.n_jobs)(
            delayed(_fit_binary)(self.estimator, X, Y[:, i], ternary=self.ternary)
            for i in range(Y.shape[1]))

        return self

    def predict(self, X, loss='soft_hamming'):
        """Predict multi-class targets using underlying estimators.

        Parameters
        ----------
        X : (sparse) array-like, shape = [n_samples, n_features]
            Data.

        loss : str or callable, default 'soft_hamming'
            Valid pre-defined loss functions are:
            ['hamming', 'soft_hamming', 'log', 'exp', 'logistic']
            Can also be a callable function that takes two binary or ternary vectors 
            and returns a scaler loss

        Returns
        -------
        y : numpy array of shape [n_samples]
            Predicted multi-class targets.
        """
        check_is_fitted(self, 'estimators_')

        if loss in LOSS_FUNCTIONS:
            loss_func = LOSS_FUNCTIONS[loss]
        elif callable(loss):
            loss_func = loss
        else:
            raise ValueError("'loss' must be a string or a callable")

        if not hasattr(self.estimator, "decision_function") \
            and loss in ('log', 'exp', 'logistic'):
            raise ValueError("The loss function '%s' is not supported"
                " for classifiers that do not implement decision_function." % loss) 

        Y = np.array([_predict_binary(e, X) for e in self.estimators_]).T

        def metric(x, y) : return np.sum(loss_func(x,y))

        pred = pairwise_distances_argmin(Y, self.code_book_, metric=metric)

        return self.classes_[pred]
