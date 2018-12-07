import io
import logging
import math
import os
import re

from numpy import NaN, sort

from .data import Dataset, Feature
from .datautils import accuracy_score, log_loss, mean_squared_error, roc_auc_score, read_csv, write_csv, is_data_frame, to_data_frame
from .resources import get as rget, config as rconfig
from .utils import Namespace, backup_file, memoize, datetime_iso

log = logging.getLogger(__name__)

# TODO: reconsider organisation of output files:
#   predictions: add framework version to name, timestamp? group into subdirs?
#   gather scores in one single file?


class Scoreboard:

    results_file = 'results.csv'

    @classmethod
    def all(cls, scores_dir=None):
        return cls(scores_dir=scores_dir)

    @classmethod
    def from_file(cls, path):
        folder, basename = os.path.split(path)
        framework_name = None
        benchmark_name = None
        task_name = None
        patterns = [
            cls.results_file,
            r"(?P<framework>[\w\-]+)_benchmark_(?P<benchmark>[\w\-]+).csv",
            r"benchmark_(?P<benchmark>[\w\-]+).csv",
            r"(?P<framework>[\w\-]+)_task_(?P<task>[\w\-]+).csv",
            r"task_(?P<task>[\w\-]+).csv",
            r"(?P<framework>[\w\-]+).csv",
        ]
        found = False
        for pat in patterns:
            m = re.fullmatch(pat, basename)
            if m:
                found = True
                d = m.groupdict()
                benchmark_name = 'benchmark' in d and d['benchmark']
                task_name = 'task' in d and d['task']
                framework_name = 'framework' in d and d['framework']
                break

        if not found:
            return None

        scores_dir = None if path == basename else folder
        return cls(framework_name=framework_name, benchmark_name=benchmark_name, task_name=task_name, scores_dir=scores_dir)

    @staticmethod
    def load_df(file):
        name = file if isinstance(file, str) else type(file)
        log.debug("Loading scores from %s", name)
        exists = isinstance(file, io.IOBase) or os.path.isfile(file)
        df = read_csv(file) if exists else to_data_frame({})
        log.info("Loaded scores from %s", name)
        return df

    @staticmethod
    def save_df(data_frame, path, append=False):
        exists = os.path.isfile(path)
        new_format = False
        if exists:
            # todo: detect format change, i.e. data_frame columns are different or different order from existing file
            pass
        if new_format or (exists and not append):
            backup_file(path)
        new_file = not exists or not append or new_format
        is_default_index = data_frame.index.name is None and not any(data_frame.index.names)
        log.debug("Saving scores to %s", path)
        write_csv(data_frame,
                  file=path,
                  header=new_file,
                  index=not is_default_index,
                  append=not new_file)
        log.info("Scores saved to %s", path)

    def __init__(self, scores=None, framework_name=None, benchmark_name=None, task_name=None, scores_dir=None):
        self.framework_name = framework_name
        self.benchmark_name = benchmark_name
        self.task_name = task_name
        self.scores_dir = scores_dir if scores_dir else rconfig().scores_dir
        self.scores = scores if scores is not None else self._load()

    @memoize
    def as_data_frame(self):
        # index = ['task', 'framework', 'fold']
        index = []
        df = self.scores if is_data_frame(self.scores) \
            else to_data_frame([dict(sc) for sc in self.scores])
        if df.empty:
            # avoid dtype conversions during reindexing on empty frame
            return df
        # fixed_cols = ['result', 'mode', 'version', 'utc']
        fixed_cols = ['task', 'framework', 'fold', 'result', 'mode', 'version', 'utc']
        fixed_cols = [col for col in fixed_cols if col not in index]
        dynamic_cols = [col for col in df.columns if col not in index and col not in fixed_cols]
        dynamic_cols.sort()
        df = df.reindex(columns=[]+fixed_cols+dynamic_cols)
        log.debug("scores columns: %s", df.columns)
        return df

    def _load(self):
        return self.load_df(self._score_file())

    def save(self, append=False):
        self.save_df(self.as_data_frame(), path=self._score_file(), append=append)

    def append(self, board_or_df):
        to_append = board_or_df.as_data_frame() if isinstance(board_or_df, Scoreboard) else board_or_df
        scores = self.as_data_frame().append(to_append, sort=False)
        return Scoreboard(scores=scores,
                          framework_name=self.framework_name,
                          benchmark_name=self.benchmark_name,
                          task_name=self.task_name,
                          scores_dir=self.scores_dir)

    def _score_file(self):
        if self.framework_name:
            if self.task_name:
                file_name = "{framework}_task_{task}.csv".format(framework=self.framework_name, task=self.task_name)
            elif self.benchmark_name:
                file_name = "{framework}_benchmark_{benchmark}.csv".format(framework=self.framework_name, benchmark=self.benchmark_name)
            else:
                file_name = "{framework}.csv".format(framework=self.framework_name)
        else:
            if self.task_name:
                file_name = "task_{task}.csv".format(task=self.task_name)
            elif self.benchmark_name:
                file_name = "benchmark_{benchmark}.csv".format(benchmark=self.benchmark_name)
            else:
                file_name = Scoreboard.results_file

        return os.path.join(self.scores_dir, file_name)


class TaskResult:

    @staticmethod
    def load_predictions(predictions_file):
        log.info("Loading predictions from %s", predictions_file)
        if os.path.isfile(predictions_file):
            df = read_csv(predictions_file)
            log.debug("Predictions preview:\n %s\n", df.head(10).to_string())
            if df.shape[1] > 2:
                return ClassificationResult(df)
            else:
                return RegressionResult(df)
        else:
            log.warning("Predictions file {file} is missing: framework either failed or could not produce any prediction".format(
                file=predictions_file,
            ))
            return NoResult()

    @staticmethod
    def save_predictions(dataset: Dataset, predictions_file: str,
                         class_probabilities=None, class_predictions=None, class_truth=None,
                         class_probabilities_labels=None,
                         classes_are_encoded=False):
        """ Save class probabilities and predicted labels to file in csv format.

        :param dataset:
        :param predictions_file:
        :param class_probabilities:
        :param class_predictions:
        :param class_truth:
        :param class_probabilities_labels:
        :param classes_are_encoded:
        :return: None
        """
        log.debug("Saving predictions to %s", predictions_file)
        prob_cols = class_probabilities_labels if class_probabilities_labels else dataset.target.label_encoder.classes
        df = to_data_frame(class_probabilities, columns=prob_cols)
        if class_probabilities_labels:
            df = df[sort(prob_cols)]  # reorder columns alphabetically: necessary to match label encoding

        predictions = class_predictions
        truth = class_truth if class_truth is not None else dataset.test.y
        if not encode_predictions_and_truth and classes_are_encoded:
            predictions = dataset.target.label_encoder.inverse_transform(class_predictions)
            truth = dataset.target.label_encoder.inverse_transform(truth)
        if encode_predictions_and_truth and not classes_are_encoded:
            predictions = dataset.target.label_encoder.transform(class_predictions)
            truth = dataset.target.label_encoder.transform(truth)

        df = df.assign(predictions=predictions)
        df = df.assign(truth=truth)
        log.info("Predictions preview:\n %s\n", df.head(20).to_string())
        backup_file(predictions_file)
        write_csv(df, file=predictions_file, index=False)
        log.info("Predictions saved to %s", predictions_file)

    @classmethod
    def score_from_predictions_file(cls, path):
        folder, basename = os.path.split(path)
        pattern = r"(?P<framework>[\w\-]+?)_(?P<task>[\w\-]+)_(?P<fold>\d+)(_(?P<datetime>\d{8}T\d{6}))?.csv"
        m = re.fullmatch(pattern, basename)
        if not m:
            log.error("%s predictions file name has wrong format", path)
            return None

        d = m.groupdict()
        framework_name = d['framework']
        task_name = d['task']
        fold = int(d['fold'])
        result = cls.load_predictions(path)
        task_result = cls(task_name, fold)
        return task_result.compute_scores(framework_name, result.metrics, result=result)

    def __init__(self, task_name: str, fold: int, predictions_dir=None):
        self.task = task_name
        self.fold = fold
        self.predictions_dir = predictions_dir if predictions_dir else rconfig().predictions_dir

    @memoize
    def get_result(self, framework_name):
        return self.load_predictions(self._predictions_file(framework_name))

    def compute_scores(self, framework_name, metrics, result=None):
        framework_def, _ = rget().framework_definition(framework_name)
        scores = Namespace(
            framework=framework_name,
            version=framework_def.version,
            task=self.task,
            fold=self.fold,
            mode=rconfig().run_mode,    # fixme: at the end, we're always running in local mode!!!
            utc=datetime_iso()
        )
        result = self.get_result(framework_name) if result is None else result
        for metric in metrics:
            score = result.evaluate(metric)
            scores[metric] = score
        scores.result = scores[metrics[0]]
        log.info("metric scores: %s", scores)
        return scores

    def _predictions_file(self, framework_name):
        return os.path.join(self.predictions_dir, "{framework}_{task}_{fold}.csv").format(
            framework=framework_name.lower(),
            task=self.task,
            fold=self.fold
        )


class Result:

    def __init__(self, predictions_df):
        self.df = predictions_df
        self.truth = self.df.iloc[:, -1].values
        self.predictions = self.df.iloc[:, -2].values
        self.target = None
        self.type = None
        self.metrics = ['acc', 'logloss', 'mse', 'rmse', 'auc']

    def acc(self):
        return float(accuracy_score(self.truth, self.predictions))

    def logloss(self):
        return float(log_loss(self.truth, self.predictions))

    def mse(self):
        return float(mean_squared_error(self.truth, self.predictions))

    def rmse(self):
        return math.sqrt(self.mse())

    def auc(self):
        return NaN

    def evaluate(self, metric):
        if hasattr(self, metric):
            return getattr(self, metric)()
        raise ValueError("Metric {metric} is not supported for {type}".format(metric=metric, type=self.type))


class NoResult(Result):

    def __init__(self):
        self.missing_result = 'NA'

    def acc(self):
        return self.missing_result

    def logloss(self):
        return self.missing_result

    def mse(self):
        return self.missing_result

    def rmse(self):
        return self.missing_result

    def auc(self):
        return self.missing_result


class ClassificationResult(Result):

    def __init__(self, predictions_df):
        super().__init__(predictions_df)
        self.classes = self.df.columns[:-2].values.astype(str)
        self.probabilities = self.df.iloc[:, :-2].values.astype(float)
        self.target = Feature(0, 'class', 'categorical', self.classes, is_target=True)
        self.type = 'binomial' if len(self.classes) == 2 else 'multinomial'
        self.truth = self._autoencode(self.truth)
        self.predictions = self._autoencode(self.predictions)

    def auc(self):
        if self.type != 'binomial':
            raise ValueError("AUC metric is only supported for binary classification: {}".format(self.classes))
        return float(roc_auc_score(self.truth, self.probabilities[:, 1]))

    def logloss(self):
        # truth_enc = self.target.label_binarizer.transform(self.truth)
        return float(log_loss(self.truth, self.probabilities))

    def _autoencode(self, vec):
        needs_encoding = not encode_predictions_and_truth or (isinstance(vec[0], str) and not vec[0].isdigit())
        return self.target.label_encoder.transform(vec) if needs_encoding else vec


class RegressionResult(Result):

    def __init__(self, predictions_df):
        super().__init__(predictions_df)
        self.truth = self.truth.astype(float)
        self.target = Feature(0, 'target', 'real', is_target=True)
        self.type = 'regression'


encode_predictions_and_truth = False


def save_predictions_to_file(dataset: Dataset, output_file: str,
                             class_probabilities=None, class_predictions=None, class_truth=None,
                             class_probabilities_labels=None,
                             classes_are_encoded=False):
    TaskResult.save_predictions(dataset, predictions_file=output_file,
                                class_probabilities=class_probabilities, class_predictions=class_predictions, class_truth=class_truth,
                                class_probabilities_labels=class_probabilities_labels,
                                classes_are_encoded=classes_are_encoded)
