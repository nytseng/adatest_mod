import numpy as np
import re
import logging
import uuid
import itertools
import sentence_transformers
import openai
import scipy.stats
#from transformers.tokenization_utils_base import ExplicitEnum
#from ._explorer import file_log

log = logging.getLogger(__name__)

class Scorer():
    pass

# <option>should not be</option>
#                     <option>should be</option>
#                     <option>should be the same as for</option>
#                     <option>should not be less than for</option>

class DummyScorer(Scorer):
    def __init__(self):
        self._id = uuid.uuid4().hex
    def __call__(self, tests):
        out = []
        for k, test in tests.iterrows():
            try:
                score = float(test.value2)
            except:
                score = np.nan
            out.append(score)
        return np.array(out)

class ClassifierScorer(Scorer):
    """ Wraps a model and defines a callable scorer that returns a score value for any input/output pair.

    Positive scores indicate test failures, positive scores indicate tests that pass. For example if we wrap
    a text sentiment classifer the `scorer(TestTree([("this is great!", "should be", "POSITIVE")]))` will return
    a large positive value indicating that the model is very likely to correctly produce that output when given
    that input.
    """

    def __init__(self, model, topk=1, output_names=None, method="dirichlet", dirichlet_concentration=10):
        """ Create a new scorer given a model that returns a probability vector for each input string.
        
        Parameters:
        -----------
        model : callable
            A model that is callable with a single argument (which is a list of strings) and returns a matrix of outputs.

        topk : int
            The number of top outputs to consider when scoring tests. For example topk=2 causes "should not be" tests to
            check the top two model outputs.

        output_names : list of strings
            A list of strings that correspond to the outputs of the model. If None, model.output_names is used.

        method : 'margin' or 'dirichlet'
            The scoring method to use. Dirichlet is preferred, but margin is available for backwards compatibility.

        dirichlet_concentration : float
            The concentration parameter for the dirichlet scoring method. It is in the units o pseudo-counts where larger
            values lead to a tighter prior centered around the model's probability outputs (so scores are more likely to
            be -1 or +1).
        """

        self._id = uuid.uuid4().hex
        self.model = model
        self.output_names = output_names
        if self.output_names is None:
            self.output_names = self.model.output_names
        if not callable(self.output_names):
            self._output_name_to_index = {v: i for i, v in enumerate(self.output_names)}
        self.topk = topk
        self.output_type = "classification"
        self.method = method
        self.dirichlet_concentration = dirichlet_concentration

    def __call__(self, tests):
        """ Score a set of tests.

        Parameters
        ----------
        tests : pandas.DataFrame
            A dataframe of tests.
        """
        if self.output_type == "classification":
            eval_inputs = []
            eval_inds = []
            variations1 = []
            variations2 = []
            for i, (k, test) in enumerate(tests.iterrows()):
                if test.comparator == "should not be" or test.comparator == "should be":
                    v1 = expand_template(test.value1)
                    for s1 in v1:
                        eval_inputs.append(s1)
                        eval_inds.append(i)
                    variations1.append(v1)
                    variations2.append(None)
                elif test.comparator == "should be the same as for":
                    # eval_inputs.append(test.value1)
                    # eval_inputs.append(test.value2)
                    v1 = expand_template(test.value1)
                    v2 = expand_template(test.value2)
                    for s1 in v1:
                        for s2 in v2:
                            eval_inputs.append(s1)
                            eval_inputs.append(s2)
                            eval_inds.append(i)
                            eval_inds.append(i)
                    variations1.append(v1)
                    variations2.append(v2)

            try:
                model_out = self.model(eval_inputs)
            except Exception as e:
                model_out = np.zeros((len(eval_inputs), len(self.model.output_names))) * np.nan # TODO: remove this hack after the user study
                log.error(e)
                log.error(eval_inputs)
                log.error("The model threw an exception when evaluating inputs! We are patching this disaster with np.nan for the sake of the user study!")

            out = [[] for _ in range(tests.shape[0])]
            out_pos = 0
            i = 0
            value1_outputs = [{} for _ in range(tests.shape[0])]
            value2_outputs = [{} for _ in range(tests.shape[0])]
            while i < len(model_out):
                out_pos = eval_inds[i]

                comparator = tests.iloc[out_pos]["comparator"]
                if comparator == "should not be" or comparator == "should be":

                    # save the top model outputs
                    inds = np.argsort(-model_out[i])
                    shown_tmp = {}
                    for j in inds[:5]:
                        shown_tmp[self.model.output_names[j]] = float(model_out[i][j])
                    value1_outputs[out_pos] = shown_tmp

                    token_to_check = tests.iloc[out_pos]['value2']

                    # TODO: This is a hack where we're looking for different capitalizations of the output if the original one doesn't exist
                    # we added this because of gpt-2 word splitting (which makes 'muslim' not be in the output)
                    # we should fix this at some point :P
                    if token_to_check not in self._output_name_to_index:
                        if token_to_check.capitalize() in self._output_name_to_index:
                            token_to_check = token_to_check.capitalize()
                        elif token_to_check.lower() in self._output_name_to_index:
                            token_to_check = token_to_check.lower()
                    
                    # multiple tokens can be checked at the same time with templates
                    out_val = np.nan
                    for token_part in expand_template(token_to_check):
                        ind = self._output_name_to_index.get(token_part, None)
                        if ind is not None and model_out[i] is not None:
                            sorted_values = np.argsort(model_out[i])
                            topk = topk_threshold_ind(ind, sorted_values, self.topk)

                            if self.method == "dirichlet":
                                raw_score = compute_dirichlet_score(
                                    ind, model_out[i], self.topk,
                                    concentration=self.dirichlet_concentration,
                                    # we treat values less than 10% of the topk value as unlikely to impact the results
                                    # this is used to avoid unnecessary computation
                                    domination_threshold=model_out[i][topk] / 10 
                                )

                                if comparator == "should be":
                                    score = raw_score
                                else:
                                    score = -raw_score

                            if np.isnan(model_out[i][ind]):
                                score = np.nan
                            elif model_out[i][ind] > model_out[i][topk]:
                                if self.method == "dirichlet":
                                    # mval = 1 / len(model_out[i]) if self.topk == 1 else 0 # minimum value possible while being at the top
                                    # score = (raw_score - mval) / (1 - mval) # scale from 0 to 1
                                    score = raw_score
                                else:
                                    score = model_out[i][ind] - model_out[i][topk]
                            else:
                                if self.method == "dirichlet":
                                    # mval = 1 / (self.topk + 1) # maximum value possible while not being at the top
                                    # score = (raw_score - 1 + mval) / (1 - mval) # scale from 0 to 1
                                    score = raw_score - 1
                                else:
                                    mask = (model_out[i] <= model_out[i][topk]) & (model_out[i] > model_out[i][ind])
                                    score = (model_out[i][ind] - model_out[i][mask]).sum()
                            if comparator == "should be":
                                score *= -1
                            # out_val = max(score, out_val)
                            out[out_pos].append(score)
                    # out[out_pos] = max(out[out_pos], out_val)
                    i += 1
                elif comparator == "should be the same as for":

                    # save the top model outputs
                    inds = np.argsort(-model_out[i])
                    shown_tmp = {}
                    for j in inds[:5]:
                        shown_tmp[self.model.output_names[j]] = float(model_out[i][j])
                    value1_outputs[out_pos] = shown_tmp
                    inds = np.argsort(-model_out[i+1])
                    shown_tmp = {}
                    for j in inds[:5]:
                        shown_tmp[self.model.output_names[j]] = float(model_out[i+1][j])
                    value2_outputs[out_pos] = shown_tmp
                    
                    if self.method == "dirichlet":
                        score = compute_dirichlet_equality_score(
                            model_out[i], model_out[i+1], self.topk, concentration=self.dirichlet_concentration,
                            # we treat values less than 1% of the top value as unlikely to impact the results
                            # this is used to avoid unnecessary computation (note this is used more aggressivly than for the non-equality score)
                            domination_threshold=min(np.max(model_out[i]), np.max(model_out[i+1])) / 100
                        )
                    else:
                        score = equality_score(model_out[i], model_out[i+1])
                    # out[out_pos] = max(out[out_pos], score)
                    out[out_pos].append(score)
                    i += 2
                else:
                    raise Exception(f"Comparator type '{comparator}' not yet supported!")

                # out_pos += 1
            return {
                "scores": out,
                "value1_outputs": value1_outputs,
                "value2_outputs": value2_outputs
            }
        else:
            raise Exception(f"Output type {self.output_type} not yet supported!")

    def suggest_outputs(self, current, num_suggestions=20):
        prompt = ""
        for c in current:
            prompt += '"'+c+'"\n'
        prompt += '"{output}'
        response = openai.Completion.create(
            engine='curie-instruct-beta', prompt=[prompt.format(output=o) for o in self.output_names], max_tokens=0, # self.engine
            temperature=0, n=1, stop='\"', logprobs=0, echo=True
        )
        lines = [sum(choice["logprobs"]["token_logprobs"][11:]) for choice in response["choices"]]
        pairs = list([v for v in zip(lines, self.output_names) if v[1] not in current])
        pairs.sort()
        return [v[1] for v in list(reversed(pairs))[:num_suggestions]]
        
TextScorer = ClassifierScorer

# class GeneratorScorer(Scorer):
#     """ Wraps a model and defines a callable scorer that returns a score value for any input/output pair.
#     """

#     def __init__(self, model):
#         self._id = uuid.uuid4().hex
#         self.model = model

#     def __call__(self, tests):
#         eval_inputs = []
#         eval_inds = []
#         variations1 = []
#         variations2 = []
#         for i, (k, test) in enumerate(tests.iterrows()):
#             if test.comparator == "should not be" or test.comparator == "should be":
#                 v1 = expand_template(test.value1)
#                 for s1 in v1:
#                     eval_inputs.append(s1)
#                     eval_inds.append(i)
#                 variations1.append(v1)
#                 variations2.append(None)
#             elif test.comparator == "should be the same as for":
#                 # eval_inputs.append(test.value1)
#                 # eval_inputs.append(test.value2)
#                 v1 = expand_template(test.value1)
#                 v2 = expand_template(test.value2)
#                 for s1 in v1:
#                     for s2 in v2:
#                         eval_inputs.append(s1)
#                         eval_inputs.append(s2)
#                         eval_inds.append(i)
#                         eval_inds.append(i)
#                 variations1.append(v1)
#                 variations2.append(v2)

#         try:
#             model_out = self.model(eval_inputs)
#         except Exception as e:
#             model_out = ["ERROR" for _ in range(len(eval_inputs))]#np.zeros((len(eval_inputs), len(self.model.output_names))) * np.nan # TODO: remove this hack after the user study
#             log.error(e)
#             log.error(eval_inputs)
#             log.error("The model threw an exception when evaluating inputs! We are patching this disaster with 'ERROR' for the sake of the user study!")

#         out = [[] for _ in range(tests.shape[0])]
#         out_pos = 0
#         i = 0
#         value1_outputs = [{} for _ in range(tests.shape[0])]
#         value2_outputs = [{} for _ in range(tests.shape[0])]
#         while i < len(model_out):
#             out_pos = eval_inds[i]

#             comparator = tests.iloc[out_pos]["comparator"]
#             if comparator == "should not be" or comparator == "should be":
                
#                 # auto fill missing outputs
#                 if tests.iloc[out_pos]['value2'] is None:
#                     tests.iloc[out_pos]['value2'] = model_out[i]
                
#                 # save the model output
#                 value1_outputs[out_pos]  = {}
#                 value1_outputs[out_pos][model_out[i]] = 1

#                 # multiple tokens can be checked at the same time with templates
#                 for token_part in expand_template(tests.iloc[out_pos]['value2']):
#                     out[out_pos].append(1 if model_out[i] == token_part else -1)
#                 i += 1
#             elif comparator == "should be the same as for":

#                 # save the model outputs
#                 value1_outputs[out_pos]  = {}
#                 value1_outputs[out_pos][model_out[i]] = 1
#                 value2_outputs[out_pos]  = {}
#                 value2_outputs[out_pos][model_out[i+1]] = 1
                
#                 # save the score
#                 out[out_pos].append(1 if model_out[i] == model_out[i+1] else -1)
#                 i += 2
#             else:
#                 raise Exception(f"Comparator type '{comparator}' not yet supported!")

#             # out_pos += 1
#         return out, value1_outputs, value2_outputs


class GeneratorScorer(Scorer):
    """ Wraps a model and defines a callable scorer that returns a score value for any input/output pair.
    """

    def __init__(self, model, reverse_model=None, embedding_model=None, similarity_threshold=0.9):
        self._id = uuid.uuid4().hex
        self.model = model
        self.reverse_model = reverse_model
        self.embedding_model = embedding_model
        self.similarity_threshold = similarity_threshold

    def __call__(self, tests):

        # run the model on the inputs
        eval_inputs = []
        eval_inds = []
        eval_reverse_pos = []
        variations1 = []
        variations2 = []
        for i, (k, test) in enumerate(tests.iterrows()):
            if test.comparator == "should not be" or test.comparator == "should be":
                v1 = expand_template(test.value1)
                for s1 in v1:
                    eval_inputs.append(s1)
                    eval_inds.append(i)
                variations1.append(v1)
                variations2.append(None)
            elif test.comparator == "should be invertable.":
                v1 = expand_template(test.value1)
                for s1 in v1:
                    eval_inputs.append(s1)
                    eval_inds.append(i)
                    eval_reverse_pos.append(len(eval_inputs) - 1)
            elif test.comparator == "should be the same as for":
                v1 = expand_template(test.value1)
                v2 = expand_template(test.value2)
                for s1 in v1:
                    for s2 in v2:
                        eval_inputs.append(s1)
                        eval_inputs.append(s2)
                        eval_inds.append(i)
                        eval_inds.append(i)
                variations1.append(v1)
                variations2.append(v2)
        try:
            model_out = self.model(eval_inputs)
        except Exception as e:
            model_out = ["ERROR" for _ in range(len(eval_inputs))]#np.zeros((len(eval_inputs), len(self.model.output_names))) * np.nan # TODO: remove this hack after the user study
            log.error(e)
            log.error(eval_inputs)
            log.error("The model threw an exception when evaluating inputs! We are patching this disaster with 'ERROR' for the sake of the user study!")

        # run the reverse model on any outputs we need to
        # eval_reverse_inputs = []
        
        # for i, (k, test) in enumerate(tests.iterrows()):
        #     if test.comparator == "should be invertable.":
        #         v1 = expand_template(test.value1)
        #         for s1 in v1:
        #             eval_reverse_inputs.append(s1)
        #             eval_reverse_inds.append(i)
        if len(eval_reverse_pos) > 0:
            model_reverse_out = [None for _ in model_out]
            input_embed = [None for _ in model_out]
            round_trip_embed = [None for _ in model_out]
            try:
                # compute input embedding
                tmp = self.embedding_model.encode([eval_inputs[ind] for ind in eval_reverse_pos], convert_to_tensor=True, show_progress_bar=False).cpu()
                for i, ind in enumerate(eval_reverse_pos):
                    input_embed[ind] = tmp[i]

                # compute reverse model output
                reverse_out = self.reverse_model([model_out[ind] for ind in eval_reverse_pos])
                for i, ind in enumerate(eval_reverse_pos):
                    model_reverse_out[ind] = str(reverse_out[i])

                # compute round trip embedding
                tmp = self.embedding_model.encode(reverse_out, convert_to_tensor=True, show_progress_bar=False).cpu()
                for i, ind in enumerate(eval_reverse_pos):
                    round_trip_embed[ind] = tmp[i]

            except Exception as e:
                model_reverse_out = ["ERROR" for _ in range(len(model_out))]
                log.error(e)
                log.error("The reverse model threw an exception when evaluating inputs! We are patching this disaster with 'ERROR' for the sake of the user study!")
        else:
            model_reverse_out = []

        out = [[] for _ in range(tests.shape[0])]
        out_pos = 0
        i = 0
        value1_outputs = [{} for _ in range(tests.shape[0])]
        value2_outputs = [{} for _ in range(tests.shape[0])]
        while i < len(model_out):
            out_pos = eval_inds[i]

            comparator = tests.iloc[out_pos]["comparator"]
            if comparator == "should not be" or comparator == "should be":
                
                # auto fill missing outputs
                if tests.iloc[out_pos]['value2'] is None:
                    tests.loc[tests.index[out_pos], 'value2'] = str(model_out[i])
                
                # save the model output
                value1_outputs[out_pos]  = {}
                value1_outputs[out_pos][model_out[i]] = 1

                # multiple tokens can be checked at the same time with templates
                for token_part in expand_template(tests.iloc[out_pos]['value2']):
                    out[out_pos].append(1 if model_out[i] == token_part else -1)
                i += 1
            elif comparator == "should be invertable.":
                
                # compare embedding distances
                score = sentence_transformers.util.pytorch_cos_sim(input_embed[i], round_trip_embed[i]).numpy()[0][0]
                out[out_pos].append(self.similarity_threshold-score)

                # update the output since it is always computed in inversion tests
                tests.loc[tests.index[out_pos], 'value2'] = str(model_reverse_out[i])
                
                # save the model round trip output
                value1_outputs[out_pos]  = {}
                value1_outputs[out_pos][str(model_out[i])] = 1

                i += 1
            elif comparator == "should be the same as for":

                # save the model outputs
                value1_outputs[out_pos]  = {}
                value1_outputs[out_pos][model_out[i]] = 1
                value2_outputs[out_pos]  = {}
                value2_outputs[out_pos][model_out[i+1]] = 1
                
                # save the score
                out[out_pos].append(1 if model_out[i] == model_out[i+1] else -1)
                i += 2
            else:
                raise Exception(f"Comparator type '{comparator}' not yet supported!")

            # out_pos += 1
        return {
            "scores": out,
            "value1_outputs": value1_outputs,
            "value2_outputs": value2_outputs
        }


def expand_template(s):
    """ Expand a template string into a list of strings.
    """
    # parts = []
    # for s in strings:
    matches = re.findall("{[^}]*}", s)
    s = re.sub("{[^}]*}", "{}", s)
    template_groups = [str(m)[1:-1].split("|") for m in matches]
    try:
        return [s.format(*parts) for parts in itertools.product(*template_groups)]
    except ValueError:
        return [s] # we return the template not filled in if it is invalid

def clean_template(s):
    """ This removes duplicate template entries.
    """
    matches = re.findall("{[^}]*}", s)
    s = re.sub("{[^}]*}", "{}", s)
    template_groups = [str(m)[1:-1].split("|") for m in matches]
    clean_groups = ["{"+"|".join(list({v: None for v in g}.keys()))+"}" for g in template_groups]
    try:
        return s.format(*clean_groups)
    except ValueError:
        return s # we return the template not cleaned in if it is invalid

def topk_threshold_ind(ind, sorted_values, k):
    """ Return the threshold value for which if ind dropped below it would not be in the top k (without other scores changing).
    """
    if ind in sorted_values[-k:]:
        topk = sorted_values[-k - 1]
    else:
        topk = sorted_values[-k]
    if topk == ind:
        topk = sorted_values[-k - 1]
    return topk

def compute_dirichlet_score(ind, model_output, k=1, concentration=10, empirical_samples=1000, domination_threshold=1e-5):
    """ Compute the probability that ind is in the top k set of probabilities.

    This is done by sampling from a Dirichlet distribution with concentration parameter centered around the model output
    and assuming a concentration weight of 10 pseudo-counts.

    Parameters
    ----------
    ind : int
        The index to compute the score for.

    model_output : np.array
        The model output probabilities.

    k : int
        The number of top k probabilities to consider ind in.

    concentration : float
        The concentration parameter for the Dirichlet distribution. Larger values make the distribution have lower variance.

    empirical_samples : int
        We can't calculate the probability of ind being in the top k set of probabilities exactly, so we sample from the
        distribution to get an empirical estimate. This controls the number of samples to take.

    domination_threshold : float
        Below this value we assume that these output dims can be safely ignored. This is used to avoid unnessecary computation.
    """
    
    # if our output of interest is dominated then we just skip the work and return 0
    if model_output[ind] < domination_threshold:
        return 0
    
    # shrink the number of dims we have to deal with by collapsing low probability dims
    bundles = []
    bundle = []
    bundle_sizes = []
    inds = np.argsort(model_output)
    bundle_size = 0
    new_ind = -1
    for i,sind in enumerate(inds):
        if bundle_size + model_output[sind] < domination_threshold:
            bundle.append(sind)
            bundle_size += model_output[sind]
        else:
            if len(bundle) > 0:
                bundles.append(bundle)
                bundle_sizes.append(bundle_size)

            if sind == ind:
                new_ind = len(bundles)
            bundle = [sind]
            bundle_size = model_output[sind]
    bundles.append(bundle)
    bundle_sizes.append(bundle_size)
    
    # normalize the scores for the Dirichlet parameter
    normed_output = np.array(bundle_sizes) + 1e-6
    normed_output /= normed_output.sum()
    normed_output *= concentration
    
    if k == 1:
        sort_inds = np.argmax(scipy.stats.dirichlet.rvs(normed_output, empirical_samples, random_state=0), 1)
        return (sort_inds == new_ind).mean()
    else:
        sort_inds = np.argsort(-scipy.stats.dirichlet.rvs(normed_output, empirical_samples, random_state=0), 1)
        return ((sort_inds[:,:k] - new_ind) == 0).sum() / sort_inds.shape[0]

def compute_dirichlet_equality_score(model_output1, model_output2, k=1, concentration=10, empirical_samples=1000, domination_threshold=1e-5):
    """ Compute the probability that ind is in the top k set of probabilities.

    This is done by sampling from a Dirichlet distribution with concentration parameter centered around the model output
    and assuming a concentration weight of 10 pseudo-counts.

    Parameters
    ----------
    ind : int
        The index to compute the score for.

    model_output : np.array
        The model output probabilities.

    k : int
        The number of top k probabilities to consider ind in.

    concentration : float
        The concentration parameter for the Dirichlet distribution. Larger values make the distribution have lower variance.

    empirical_samples : int
        We can't calculate the probability of ind being in the top k set of probabilities exactly, so we sample from the
        distribution to get an empirical estimate. This controls the number of samples to take.

    domination_threshold : float
        Below this value we assume that these output dims can be safely ignored. This is used to avoid unnessecary computation.
    """

    assert len(model_output1) == len(model_output2)

    

    

    # shrink the number of dims we have to deal with by collapsing low probability dims
    used_inds = [i for i in range(len(model_output1)) if model_output1[i] > domination_threshold or model_output2[i] > domination_threshold]
    # model_output1 = model_output1[used_inds]
    # model_output2 = model_output2[used_inds]
    model_output1_padded = np.zeros(len(used_inds) + 1)
    model_output1_padded[1:] = model_output1[used_inds]
    model_output1_padded[0] = 1 - np.sum(model_output1)
    model_output2_padded = np.zeros(len(used_inds) + 1)
    model_output2_padded[1:] = model_output2[used_inds]
    model_output2_padded[0] = 1 - np.sum(model_output2)
    
    # normalize the scores for the Dirichlet parameter
    normed_output1 = np.array(model_output1_padded) + 1e-6
    normed_output1 /= normed_output1.sum()
    normed_output1 *= concentration
    normed_output2 = np.array(model_output2_padded) + 1e-6
    normed_output2 /= normed_output2.sum()
    normed_output2 *= concentration
    
    if k == 1:
        sort_inds1 = np.argmax(scipy.stats.dirichlet.rvs(normed_output1, empirical_samples, random_state=0), 1)
        sort_inds2 = np.argmax(scipy.stats.dirichlet.rvs(normed_output2, empirical_samples, random_state=0), 1)

        # the average number of matches, excluding the first position (which is a bucket for all dominated, low prob, dims)
        match_rate = ((sort_inds1 - sort_inds2 == 0) * (sort_inds1 != 0)).mean()

        if np.argmax(model_output1) == np.argmax(model_output2):
            return -match_rate
        else:
            return 1 - match_rate
    else:
        raise Exception("The 'should be the same as for' is not implemented for topk > 1!")

def equality_score(output_values1, output_values2, topk=1):
    assert topk == 1
    ind1 = np.argmax(output_values1)
    ind2 = np.argmax(output_values2)
    max1 = output_values1[ind1]
    max2 = output_values2[ind2]
    margins = np.zeros(len(output_values1))

    if ind1 != ind2:
        min_margin = 1e6
        for i in range(len(output_values1)):
            score1 = max(0, max1 - output_values1[i])
            score2 = max(0, max2 - output_values2[i])
            margin = score1 + score2
            if margin < min_margin:
                min_margin = margin
        return min_margin
    else:
        val1 = output_values1[ind1]
        output_values1[ind1] = np.nan
        score1 = val1 - np.nanmax(output_values1)
        output_values1[ind1] = val1

        val2 = output_values2[ind2]
        output_values2[ind2] = np.nan
        score2 = val2 - np.nanmax(output_values2)
        output_values2[ind2] = val2
        return -min(score1, score2)