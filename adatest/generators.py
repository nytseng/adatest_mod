""" A set of generators for AdaTest.
"""
import asyncio
import aiohttp
from profanity import profanity
import numpy as np
import os
import adatest
from .embedders import cos_sim
import urllib

try:
    import clip
    import clip_retrieval.clip_client
except ImportError:
    pass


class Generator():
    """ Abstract class for generators.
    """

    def __init__(self, source):
        """ Create a new generator from a given source.
        """
        self.source = source

    def __call__(self, prompts, topic, topic_description, mode, scorer, num_samples, max_length):
        """ Generate a set of prompts for a given topic.
        """
        raise NotImplementedError()

    def _validate_prompts(self, prompts):
        """ Ensure the passed prompts are a list of prompt lists.
        """
        if len(prompts[0]) > 0 and isinstance(prompts[0][0], str):
            prompts = [prompts]
        
        # Split apart prompt IDs and prompts
        prompt_ids, trimmed_prompts = [], []
        for prompt in prompts:
            prompt_without_id = []
            for entry in prompt:
                prompt_ids.append(entry[0])
                prompt_without_id.append(entry[1:])
            trimmed_prompts.append(prompt_without_id)

        return trimmed_prompts, prompt_ids


class TextCompletionGenerator(Generator):
    """ Abstract class for generators.
    """
    
    def __init__(self, source, sep, subsep, quote, filter):
        """ Create a new generator with the given separators and max length.
        """
        super().__init__(source)
        self.sep = sep
        self.subsep = subsep
        self.quote = quote
        self.filter = filter

        # value1_format = '"{value1}"'
        # value2_format = '"{value1}"'
        # value1_value2_format = '"{value1}" {comparator} "{value2}"'
        # value1_comparator_value2_format = '"{value1}" {comparator} "{value2}"'
        # value1_comparator_format = '"{value1}" {comparator} "{value2}"'
        # comparator_value2_format = '"{value1}" {comparator} "{value2}"'

    def __call__(self, prompts, topic, topic_description, max_length=None):
        """ This should be overridden by concrete subclasses.

        Parameters:
        -----------
        prompts: list of tuples, or list of lists of tuples
        """
        pass
    
    def _varying_values(self, prompts, topic):
        """ Marks with values vary in the set of prompts.
        """

        show_topics = False
        # gen_value1 = False
        # gen_value2 = False
        # gen_value3 = False
        for prompt in prompts:
            topics, inputs = zip(*prompt)
            show_topics = show_topics or len(set(list(topics) + [topic])) > 1
            # gen_inputs = gen_inputs or len(set(inputs)) > 1
            # gen_value2 = False
            # gen_value3 = False
        return show_topics#, gen_value1, gen_value2, gen_value3
    
    def _create_prompt_strings(self, prompts, topic, content_type):
        """ Convert prompts that are lists of tuples into strings for the LM to complete.
        """

        assert content_type in ["tests", "topics"], "Invalid mode: {}".format(content_type)

        show_topics = self._varying_values(prompts, topic) or content_type == "topic"

        prompt_strings = []
        for prompt in prompts:
            prompt_string = ""
            for p_topic, input in prompt:
                if show_topics:
                    if content_type == "tests":
                        prompt_string += self.sep + p_topic + ":" + self.sep + self.quote
                    elif content_type == "topics":
                        prompt_string += "A subtopic of " + self.quote + p_topic + self.quote + " is " + self.quote
                else:
                    prompt_string += self.quote
                
                prompt_string += input + self.quote
                prompt_string += self.sep
            if show_topics:
                if content_type == "tests":
                    prompt_strings.append(prompt_string + self.sep + topic + ":" + self.sep + self.quote)
                elif content_type == "topics":
                    prompt_strings.append(prompt_string + "A subtopic of " + self.quote + topic + self.quote + " is " + self.quote)
            else:
                prompt_strings.append(prompt_string + self.quote)
        return prompt_strings
    
    def _parse_suggestion_texts(self, generated_tests, prompts):
        """ Parse the suggestion texts into tuples.
        """
        import re
        assert len(generated_tests) % len(prompts) == 0, "Missing prompt completions!"

        # _, gen_value1, gen_value2, gen_value3 = self._varying_values(prompts, "") # note that "" is an unused topic argument
        
        num_samples = len(generated_tests) // len(prompts)
        valid_tests = []
        for i, tests in enumerate(generated_tests):
            # if callable(self.filter):
            #     test_str = self.filter(generated_tests)
            sentence_counter = 1

            if bool(re.search(r'\d', tests)): # if the contains any integers.
                while True:
                    split_tok = str(sentence_counter)+". "
                    if sentence_counter > 5 or len(valid_tests) >= 5: # limit to 5 generations
                        print("exceeded 5 tests, return early")
                        return valid_tests

                    print("next split_tok" + split_tok)
                    if split_tok in tests:
                        split_list = tests.split(split_tok)
                        print("SPLITTING TESTS by " + split_tok)
                        print(split_list[0])
                        temp_sentence = split_list[0]
                        if temp_sentence != None and len(temp_sentence) > 8:
                            valid_tests.append(temp_sentence)
                            print("valid test: " + temp_sentence)

                        tests = split_list[1]
                        print(tests)

                        sentence_counter += 1
                    return valid_tests
            else: # parse without integers
                suggestions = tests.split(". ")
                print("SPLITTING TESTS MANUALLY")
                valid_tests.extend(suggestions)
        return valid_tests 

class HuggingFace(TextCompletionGenerator):
    """This class exists to embed the StopAtSequence class."""
    import transformers

    def __init__(self, source, sep, subsep, quote, filter):
        super().__init__(source, sep, subsep, quote, filter)
        

    class StopAtSequence(transformers.StoppingCriteria):
        def __init__(self, stop_string, tokenizer, window_size=10):
            self.stop_string = stop_string
            self.tokenizer = tokenizer
            self.window_size = 10
            self.max_length = None
            self.prompt_length = 0
            
        def __call__(self, input_ids, scores):
            if len(input_ids[0]) > self.max_length + self.prompt_length:
                return True

            # we need to decode rather than check the ids directly because the stop_string may get enocded differently in different contexts
            return self.tokenizer.decode(input_ids[0][-self.window_size:])[-len(self.stop_string):] == self.stop_string

           
class Transformers(HuggingFace):
    def __init__(self, model, tokenizer, sep="\n", subsep=" ", quote="\"", filter=profanity.censor):
        # TODO [Harsha]: Add validation logic to make sure model is of supported type.
        super().__init__(model, sep, subsep, quote, filter)
        self.gen_type = "model"
        self.tokenizer = tokenizer
        self.device = self.source.device

        self._sep_stopper = HuggingFace.StopAtSequence(self.quote+self.sep, self.tokenizer)
    
    def __call__(self, prompts, topic, topic_description, mode, scorer=None, num_samples=1, max_length=100):
        prompts, prompt_ids = self._validate_prompts(prompts)
        if len(prompts) == 0:
            raise ValueError("ValueError: Unable to generate suggestions from completely empty TestTree. Consider writing a few manual tests before generating suggestions.") 
        prompt_strings = self._create_prompt_strings(prompts, topic, mode)
        
        # monkey-patch a method that prevents the use of past_key_values
        saved_func = self.source.prepare_inputs_for_generation
        def prepare_inputs_for_generation(input_ids, **kwargs):
            if "past_key_values" in kwargs:
                return {"input_ids": input_ids, "past_key_values": kwargs["past_key_values"]}
            else:
                return {"input_ids": input_ids}
        self.source.prepare_inputs_for_generation = prepare_inputs_for_generation
        
        # run the generative LM for each prompt
        print("Prompt Strings:")
        print(prompt_strings)
        suggestion_texts = []
        for prompt_string in prompt_strings:
            input_ids = self.tokenizer.encode(prompt_string, return_tensors='pt').to(self.device)
            cache_out = self.source(input_ids[:, :-1], use_cache=True)

            print("Model Output:")
            print(cache_out)

            for _ in range(num_samples):
                self._sep_stopper.prompt_length = 1
                self._sep_stopper.max_length = max_length
                out = self.source.sample(
                    input_ids[:, -1:], pad_token_id=self.source.config.eos_token_id,
                    stopping_criteria=self._sep_stopper,
                    past_key_values=cache_out.past_key_values # TODO: enable re-using during sample unrolling as well
                )

                # we ignore first token because it is part of the prompt
                suggestion_text = self.tokenizer.decode(out[0][1:])
                
                # we ignore the stop string to match other backends
                if suggestion_text[-len(self._sep_stopper.stop_string):] == self._sep_stopper.stop_string:
                    suggestion_text = suggestion_text[:-len(self._sep_stopper.stop_string)]
                
                suggestion_texts.append(suggestion_text)

        # restore the old function that prevents the past_key_values argument from getting passed
        self.source.prepare_inputs_for_generation = saved_func
        
        return self._parse_suggestion_texts(suggestion_texts, prompts)


class Pipelines(HuggingFace):
    import transformers
    def __init__(self, pipeline: transformers.pipelines.base.Pipeline , sep="\n", subsep=" ", quote="\"", filter=profanity.censor):
        super().__init__(pipeline, sep, subsep, quote, filter)
        self.gen_type = "model"
        self.stop_sequence = self.quote + self.sep
        self._sep_stopper = HuggingFace.StopAtSequence(self.stop_sequence, pipeline.tokenizer)

    def __call__(self, prompts, topic, topic_description, mode, scorer=None, num_samples=1, max_length=100):
        if len(prompts) == 0:
            raise ValueError("ValueError: Unable to generate suggestions from completely empty TestTree. Consider writing a few manual tests before generating suggestions.") 
        prompts, prompt_ids = self._validate_prompts(prompts)
        prompt_strings = self._create_prompt_strings(prompts, topic, mode)

        suggestion_texts = []
        print("prompt_strings")
        print(prompt_strings)
        for p in prompt_strings:
            prompt_length = len(self.source.tokenizer.tokenize(p))
            self._sep_stopper.prompt_length = prompt_length
            self._sep_stopper.max_length = max_length
            generations = self.source(p,
                        do_sample=True,
                        max_length=prompt_length + max_length,
                        num_return_sequences=num_samples,
                        pad_token_id=self.source.model.config.eos_token_id,
                        stopping_criteria=[self._sep_stopper])
            # print("Generations:")
            # print(generations)
            for gen in generations:
                generated_text = gen['generated_text']
                print("Generated text: " + generated_text)
                # Trim off text after stop_sequence
                stop_seq_index = generated_text.rfind(self.stop_sequence)
                if (stop_seq_index != -1):
                    generated_text = generated_text[:stop_seq_index]
                elif generated_text[-1] == self.quote:
                    # Sometimes the quote is at the end without a trailing newline
                    generated_text = generated_text[:-1]
                print("Text after parsing: " + generated_text)
                suggestion_texts.append(generated_text)

        print("Returns:")
        print(self._parse_suggestion_texts(suggestion_texts, prompts))
        return self._parse_suggestion_texts(suggestion_texts, prompts)


class OpenAI(TextCompletionGenerator):
    """ Backend wrapper for the OpenAI API that exposes GPT-3.
    """
    
    def __init__(self, model="curie", api_key=None, sep="\n", subsep=" ", quote="\"", temperature=1.0, top_p=0.95, filter=profanity.censor):
        import openai

        # TODO [Harsha]: Add validation logic to make sure model is of supported type.
        super().__init__(model, sep, subsep, quote, filter)
        self.gen_type = "model"
        self.temperature = temperature
        self.top_p = top_p
        if api_key is not None:
            openai.api_key = api_key

        # load a key by default if a standard file exists
        elif openai.api_key is None:
            key_path = os.path.expanduser("~/.openai_api_key")
            if os.path.exists(key_path):
                with open(key_path) as f:
                    openai.api_key = f.read().strip()

    def __call__(self, prompts, topic, topic_description, mode, scorer, num_samples=1, max_length=100):
        import openai

        if len(prompts[0]) == 0:
            raise ValueError("ValueError: Unable to generate suggestions from completely empty TestTree. Consider writing a few manual tests before generating suggestions.") 

        prompts, prompt_ids = self._validate_prompts(prompts)
        # prompt_strings = self._create_prompt_strings(prompts, topic)

        # find out which values in the prompt have multiple values and so should be generated
        topics_vary = self._varying_values(prompts, topic)

        # create prompts to generate the model input parameters of the tests
        prompt_strings = self._create_prompt_strings(prompts, topic, mode)
        
        # call the OpenAI API to complete the prompts
        response = openai.Completion.create(
            model=self.source, prompt=prompt_strings, max_tokens=max_length, user="adatest",
            temperature=self.temperature, top_p=self.top_p, n=num_samples, stop=self.quote
        )
        suggestion_texts = [choice["text"] for choice in response["choices"]]
        
        return self._parse_suggestion_texts(suggestion_texts, prompts)


class AI21(TextCompletionGenerator):
    """ Backend wrapper for the AI21 API.
    """
    
    def __init__(self, model, api_key, sep="\n", subsep=" ", quote="\"", temperature=0.95, filter=profanity.censor):
        # TODO [Harsha]: Add validation logic to make sure model is of supported type.
        super().__init__(model, sep, subsep, quote, filter)
        self.gen_type = "model"
        self.api_key = api_key
        self.temperature = temperature
        self.event_loop = asyncio.get_event_loop()
    
    def __call__(self, prompts, topic, topic_description, mode, scorer=None, num_samples=1, max_length=100):
        prompts, prompt_ids = self._validate_prompts(prompts)
        prompt_strings = self._create_prompt_strings(prompts, topic, mode)
        
        # define an async call to the API
        async def http_call(prompt_string):
            async with aiohttp.ClientSession() as session:
                async with session.post(f"https://api.ai21.com/studio/v1/{self.source}/complete",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json={
                            "prompt": prompt_string, 
                            "numResults": num_samples, 
                            "maxTokens": max_length,
                            "stopSequences": [self.quote+self.sep],
                            "topKReturn": 0,
                            "temperature": self.temperature
                        }) as resp:
                    result = await resp.json()
                    return [c["data"]["text"] for c in result["completions"]]
        
        # call the AI21 API asyncronously to complete the prompts
        results = self.event_loop.run_until_complete(asyncio.gather(*[http_call(s) for s in prompt_strings]))
        suggestion_texts = []
        for result in results:
            suggestion_texts.extend(result)
        
        return self._parse_suggestion_texts(suggestion_texts, prompts)


class TestTreeSource(Generator):

    def __init__(self, test_tree, assistant_generator=None):
        # TODO: initialize with test tree
        super().__init__(test_tree)
        self.gen_type = "test_tree"
        self.assistant_generator = assistant_generator

    def __call__(self, prompts, topic, topic_description, test_type=None, scorer=None, num_samples=1, max_length=100): # TODO: Unify all __call__ signatures
        if len(prompts) == 0:
            # Randomly return instances without any prompts to go off of. TODO: Consider better alternatives like max-failure?
            return self.source.iloc[np.random.choice(self.source.shape[0], size=min(50, self.source.shape[0]), replace=False)]

        prompts, prompt_ids = self._validate_prompts(prompts)

        # TODO: Currently only returns valid subtopics. Update to include similar topics based on embedding distance?
        if test_type == "topics":
            proposals = []
            for id, test in self.source.iterrows():
                # check if requested topic is *direct* parent of test topic
                if test.label == "topic_marker" and topic == test.topic[0:test.topic.rfind('/')] and test.topic != "":
                    proposals.append(urllib.parse.unquote(test.topic.rsplit("/", 2)[1]))
            return proposals

        # Find tests closest to the proposals in the embedding space
        # TODO: Hallicunate extra samples if len(prompts) is insufficient for good embedding calculations.
        # TODO: Handle case when suggestion_threads>1 better than just selecting the first set of prompts as we do here
        topic_embeddings = np.vstack([adatest._embedding_cache[input] for topic,input in prompts[0]]) 
        data_embeddings = np.vstack([adatest._embedding_cache[input] for input in self.source["input"]])
        
        max_suggestions = min(num_samples * len(prompts), len(data_embeddings))
        method = 'distance_to_avg'
        if method == 'avg_distance':
            dist = cos_sim(topic_embeddings, data_embeddings)
            closest_indices = np.argpartition(dist.mean(axis=0), -max_suggestions)[-max_suggestions:]
            
        elif method == 'distance_to_avg':
            avg_topic_embedding = topic_embeddings.mean(axis=0)

            distance = cos_sim(avg_topic_embedding, data_embeddings)
            closest_indices = np.argpartition(distance, -max_suggestions)[-max_suggestions:]

        output = self.source.iloc[np.array(closest_indices).squeeze()].copy()
        output['topic'] = topic
        return output


class ClipRetrieval(Generator):
    """ Backend wrapper for the ClipRetrieval package and API.
    """
    
    def __init__(self, indice_name="laion5B", url="https://knn5.laion.ai/knn-service", use_safety_model=True, use_violence_detector=True, deduplicate=True):
        """ Build a new ClipRetrieval generator client.
        """
        super().__init__(indice_name)

        # load our CLIP embedding model
        self.clip_model, self.clip_preprocess = clip.load("ViT-L/14", device="cpu", jit=True)

        # build a ClipRetrieval client
        self.client = clip_retrieval.clip_client.ClipClient(
            url=url,
            indice_name=indice_name,
            aesthetic_weight=0,
            modality=clip_retrieval.clip_client.Modality.IMAGE,
            num_images=10, # this will get overwritten inside our __call__ method
            use_safety_model=use_safety_model,
            use_violence_detector=use_violence_detector,
            deduplicate=deduplicate
        )

    def __call__(self, prompts, topic, topic_description, mode, scorer, num_samples=1, max_length=100):
        """ Generate suggestions for the given topic and prompts.
        """

        # update the client to return the correct number of images
        self.client.num_images = num_samples

        # make sure we have valid prompts
        prompts, prompt_ids = self._validate_prompts(prompts)

        # if no topic description is provided, use the topic as the description
        # TODO: perhaps we can improve the prompt format over just /topic/subtopic here?
        if topic_description == "":
            topic_description = topic 

        # embed the text representation of the topic
        description_emb = self.get_text_embedding(topic_description)

        # embed the images in the prompts
        suggestion_texts = []
        for p in prompts:

            # we filter out all out-of-topic prompts because, unlike for text completion generators
            # we can't condition on the topic for ClipRetrieval
            prompt_embeds = [self.get_text_embedding(v[1]) for v in p if v[0].startswith(topic)]

            if len(prompt_embeds) == 0 and topic_description == "":
                raise ValueError("ValueError: Unable to generate suggestions from completely empty TestTree and no topic description. Consider adding some topics (or a topic description) before generating test suggestions.")

            # get a mean embedding for the search query
            # TODO: check if we should use spherical averaging here
            # TODO: check why the clip_retrieval API doesn't distinguish between text and image embeddings (and hence why we can average them in the same space)
            # TODO: perhaps we should model the distribution of embeddings and add some noise to the mean to get more diversity?
            mean_embedding = np.mean(np.vstack(prompt_embeds + [description_emb]), axis=0)
            
            # get the top-k images from the ClipRetrieval API
            response = self.client.query(embedding_input=mean_embedding.tolist())
            suggestion_texts.extend(["__IMAGE="+result["url"] for result in response])
        
        # return a unique list of suggestions
        # TODO: perhaps we should ensure the images are unique in content and not just url
        return list(set(suggestion_texts))
    
    def get_text_embedding(self, text):
        import torch
        with torch.no_grad():
            text_emb = self.clip_model.encode_text(clip.tokenize([text], truncate=True).to("cpu"))
            text_emb /= text_emb.norm(dim=-1, keepdim=True)
            text_emb = text_emb.cpu().detach().numpy().astype("float32")[0]
        return text_emb