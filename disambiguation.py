#!/usr/bin/env python
# -*- coding: utf-8 -*-

import inspect
import Levenshtein
from lxml import etree
import math
import models
import numpy as np
import re
from scipy import spatial
import solr
import sys
import urllib
import warnings


class Linker():

    DEBUG = False

    SOLR_SERVER = 'http://linksolr.kbresearch.nl/dbpedia/'
    SOLR_ROWS = 20

    MIN_PROB = 0.5

    query = ""
    solr_response = None
    solr_result_count = 0
    inlinks_total = 0
    score_total = 0

    entity = None
    matches = []

    model = None
    result = None
    flow = []


    def __init__(self, debug=False):

        if debug:
            self.DEBUG = debug


    def link(self, ne, ne_type=None, url=None):

        # Pre-process entity and context information
        self.entity = self.pre_process(ne, ne_type, url)
        if self.result:
            return self.result

        # If a valid entity is found, query Solr for DBpedia candidates
        self.solr_response, self.solr_result_count = self.query_solr()
        if self.result:
            return self.result

        # If any DBpedia candidates were found, continue pre-processing for feature calculation
        self.inlinks_total = self.get_total_inlinks()
        self.score_total = self.get_total_score()
        if self.entity.ne_type and self.entity.url:
            self.entity.count_quotes()

        # Initialize list of potential matches (i.e. entity-candidate combinations)
        matches = []
        for i in range(self.solr_result_count):
            description = Description(self.solr_response.results[i])
            match = Match(self.entity, description)
            matches.append(match)
        self.matches = matches

        # Calculate feature values for each match
        for match in self.matches:

            # Entity features
            match.parts = len(self.entity.ne.split())
            match.quotes = self.entity.quotes

            # Description features
            match.solr_pos = self.matches.index(match)
            if self.score_total > 0:
                match.solr_score = match.description.document.get('score') / float(self.score_total)
            match.score_local = match.solr_score
            if self.inlinks_total > 0:
                match.inlinks = match.description.document.get('inlinks') / float(self.inlinks_total)
            match.inlinks_local = match.inlinks
            match.lang = 1 if match.description.document.get('lang') == 'nl' else 0
            match.disambig = match.description.document.get('disambig')

            # String matching
            match.match_id()
            match.match_titles()
            match.match_last_part()
            match.check_name_conflict()

            # Context matching
            if self.entity.ne_type and self.entity.url:
                match.match_date()
                match.match_type()
                match.match_abstract()

        # Calculate probability for all candidates
        self.model = models.RadialSVM()
        for match in self.matches:
            example = []
            for j in range(len(self.model.features)):
                example.append(float(getattr(match, self.model.features[j])))
            match.prob = self.model.predict(example)

        # Select best candidate, if any, and return the result
        best_prob = 0
        best_match_id = -1
        for match in self.matches:
            if match.prob > best_prob:
                best_prob = match.prob
                best_match_id = self.matches.index(match)

        if best_prob >= self.MIN_PROB:
            reason = "SVM classifier best probability"
            match = self.matches[best_match_id].description.document.get('id')
            label = self.matches[best_match_id].description.label
            prob = self.matches[best_match_id].prob
            self.result = match, prob, label, reason
        else:
            reason = "SVM classifier probability too low"
            self.result = False, 0, False, reason

        if self.DEBUG:
            for match in self.matches:
                print 'id', match.description.document.get('id')
                print 'prob', match.prob

                #print 'main_title_exact_match', match.main_title_exact_match
                #print 'main_title_end_match', match.main_title_end_match
                #print 'main_title_start_match', match.main_title_start_match
                #print 'main_title_match', match.main_title_match

                #print 'title_exact_match', match.title_exact_match
                #print 'title_end_match', match.title_end_match
                #print 'title_start_match', match.title_start_match
                #print 'title_match', match.title_match
                #print 'title_match_fraction', match.title_match_fraction

                print 'quotes', match.quotes

        return self.result


    def pre_process(self, ne, ne_type=None, url=None):
        if self.DEBUG:
            self.flow.append(inspect.stack()[0][3])

        entity = Entity(ne, ne_type, url)
        if ne_type and url:
            entity.get_metadata()
            entity.get_ocr()

        if len(entity.ne) < 2 or not entity.last_part:
            reason = "Entity too short"
            self.result = None, -1.0, None, reason

        return entity


    def query_solr(self):
        if self.DEBUG:
            self.flow.append(inspect.stack()[0][3])

        # Temporary
        ne_parts = self.entity.clean_ne.split()
        last_part = None
        for part in reversed(ne_parts):
            if not part.isdigit():
                last_part = part
                break

        query = "title:\""
        query += self.entity.ne + "\" OR "
        query += "title_str:\""
        query += self.entity.clean_ne + "\""
        query += " OR lastpart_str:\""
        query += last_part + "\""

        if self.DEBUG:
            self.query = query + "&sort=lang+desc,inlinks+desc"

        self.SOLR_CONNECTION = solr.SolrConnection(self.SOLR_SERVER)

        try:
            solr_response = self.SOLR_CONNECTION.query(
                q=query, rows=self.SOLR_ROWS, indent="on",
                sort="lang,inlinks", sort_order="desc")
            numfound = solr_response.numFound
            solr_result_count = numfound if numfound <= self.SOLR_ROWS else self.SOLR_ROWS
        except Exception as error_msg:
            reason = "Failed to query solr: " + str(error_msg)
            self.result = None, -1.0, None, reason

        if solr_response is not None and solr_response.numFound == 0:
            self.result = False, 0, False, 'Nothing found'

        return solr_response, solr_result_count


    def get_total_inlinks(self):
        if self.DEBUG:
            self.flow.append(inspect.stack()[0][3])

        inlinks_total = 0
        for i in range(self.solr_result_count):
            document = self.solr_response.results[i]
            inlinks_total += document.get('inlinks')
        return inlinks_total


    def get_total_score(self):
        if self.DEBUG:
            self.flow.append(inspect.stack()[0][3])

        score_total = 0
        for i in range(self.solr_result_count):
            document = self.solr_response.results[i]
            score_total += document.get('score')
        return score_total


    def __repr__(self):
        response = str(self.result)
        if self.DEBUG:
            response = str(self.result) + ", flow: " + ":".join(self.flow)
            response += ", query: " + self.SOLR_SERVER + "select?q=" + self.query
        return response


    def __getitem__(self, count):
        if self.result:
            return [i for i in self.result][count]
        return False


class Match():

    entity = None
    description = None

    parts = 0

    solr_pos = 0
    solr_score = 0
    inlinks = 0
    lang = 0
    disambig = 0

    main_title_match = 0
    main_title_start_match = 0
    main_title_end_match = 0
    main_title_exact_match = 0
    title_match = 0
    title_start_match = 0
    title_end_match = 0
    title_exact_match = 0
    title_match_fraction = 0
    last_part_match = 0
    name_conflict = 0

    date_match = 0
    type_match = 0
    cos_sim = 0
    quotes = 0


    def __init__(self, entity, description):
        self.entity = entity
        self.description = description


    def match_id(self):
        # Use normalized title string list until they are available from the index
        # match_label = self.description.document.get('title_str')
        ne = self.entity.ne
        match_label = self.description.norm_title_str[0]
        fraction = len(ne.split()) / float(len(match_label.split()))

        if match_label == ne:
            self.main_title_exact_match = fraction
        elif match_label.endswith(ne):
            self.main_title_end_match = fraction
        elif match_label.startswith(ne):
            self.main_title_start_match = fraction
        elif match_label.find(ne) > -1:
            self.main_title_match = fraction


    def match_titles(self):
        title_match = 0
        title_start_match = 0
        title_end_match = 0
        title_exact_match = 0

        non_empty = 0
        non_matching = 0

        # Use normalized title string list until they are available from the index
        # match_label = self.description.document.get('title_str')
        ne = self.entity.ne
        match_label = self.description.norm_title_str

        for l in match_label:
            if len(l) > 0:
                fraction = len(ne.split()) / float(len(l.split()))
                non_empty += 1

                if l == ne:
                    title_exact_match += fraction
                elif l.endswith(ne):
                    title_end_match += fraction
                elif l.startswith(ne):
                    title_start_match += fraction
                elif l.find(ne) > -1:
                    title_match += fraction

        self.title_match = title_match
        self.title_start_match = title_start_match
        self.title_end_match = title_end_match
        self.title_exact_match = title_exact_match

        total_matching = title_exact_match + title_end_match + title_start_match + title_match
        self.title_match_fraction = total_matching / float(non_empty)


    def match_last_part(self):
        ne = self.entity.ne
        match_label = self.description.norm_title_str

        # If the entity consists of a single word
        if not len(ne.split()) > 1:
            # The main title must be longer than the ne
            if len(match_label[0]) >= len(ne):
                # The main title must start or end with the ne
                if (match_label[0].startswith(ne) or match_label[0].endswith(ne)):
                    # At least one title must contain the ne and not contain an opening bracket
                    if True in [m.find(ne) > -1 and not m.find('(') > -1 for m in match_label]:
                        # The main title itself consists of a single word or the last word is the ne and it doesn't contain 'et'
                        if len(match_label[0].split()) == 1 or (match_label[0].split()[-1] == ne and not match_label[0].find(' et ') > -1):
                            self.last_part_match = 1

        # If the entity consists of multiple words
        else:
            # If the ne has more parts than the main label or the first part has more than two letters (i.e. not an initial) and differs from
            # the first part of the main label
            if len(match_label[0].split()) < len(ne.split()) or (len(ne.split()[0]) > 2 and not ne.split()[0] == match_label[0].split()[0]):
                # And the initial letters of the main label and ne parts are not identical
                if not [i[0] for i in match_label[0].split()] == [i[0] for i in ne.split()]:
                    # No last part match can be made, so return
                    return

            # Else, so the main label has at least as many parts as the ne and the first part has only one or two letters
            for label in match_label:

                # Skip empty titles
                if not label.strip():
                    continue

                # If the title ends with the last part of the ne
                if label.endswith(ne.split()[-1]):
                    # If the title and the ne have the same amount of parts
                    if len(ne.split()) == len(label.split()):
                        # And the initial letters don't match
                        if not [i[0] for i in ne.split()] == [i[0] for i in label.split()]:
                            # No last part match can be made, so continue to the next title
                            continue

                    # If the title has more parts than the ne
                    else:
                        skip = False
                        for i in ne.split():
                            # Each initial letter of the ne parts must appear
                            # as initial letter of a title part
                            if not i[0] in [i[0] for i in label.split()]:
                                skip = True
                                break
                        if skip:
                            continue

                    # Now that initials match, check if the part length
                    # indicates the use of initials
                    if True in [len(l) <= 2 for l in ne.split()]:
                        self.last_part_match = 1
                        break


    def check_name_conflict(self):
        if not self.title_exact_match:
            if not (self.title_start_match > 0 and self.title_end_match > 0):
                if not (len(self.entity.ne.split()) > 1 and self.title_start_match > 0):
                    if not self.last_part_match:
                        self.name_conflict = 1


    def match_date(self):
        if self.entity.publ_date:
            year_of_publ = int(self.entity.publ_date[:4])
            year_of_birth = self.description.document.get('yob')
            if year_of_birth is not None:
                if year_of_publ < year_of_birth:
                    self.date_match = -1
                else:
                    self.date_match = 1


    def match_type(self):

        TPTA_SCHEMA_MAPPING = {'person': 'Person', 'location': 'Place', 'organisation': 'Organization'}

        if self.entity.ne_type in TPTA_SCHEMA_MAPPING:
            schema_types = self.description.document.get('schemaorgtype')
            if schema_types:
                for t in schema_types:
                    if t == TPTA_SCHEMA_MAPPING[self.entity.ne_type]:
                        self.type_match = 1
                        break


    def match_abstract(self):
        warnings.filterwarnings('ignore', message='.*Unicode equal comparison.*')
        abstract = self.description.document.get('abstract')
        ocr = self.entity.ocr

        if ocr and abstract:
            corpus = [ocr, abstract]

            # Tokenize both documents into bow's
            punctuation = [',', '.']
            bow = []
            for d in corpus:
                for p in punctuation:
                    d = d.replace(p, '')
                d = d.lower()
                d = d.split()
                bow.append(d)

            # Build vocabulary of words of at least 5 characters
            voc = []
            for b in bow:
                for t in b:
                    if not t in voc and len(t) >= 5:
                        voc.append(t)

            # Create normalized word count vectors for both documents
            vec = []
            for b in bow:
                v = np.zeros(len(voc))
                for t in voc:
                    v[voc.index(t)] = b.count(t)
                v_norm = v / np.linalg.norm(v)
                vec.append(v_norm)

            # Calculate the distance between the resulting vectors
            self.cos_sim = 1 - spatial.distance.cosine(vec[0], vec[1])


class Description():

    document = None
    norm_title_str = []
    label = ''


    def __init__(self, doc):
        self.document = doc
        self.label = doc.get('title')[0]

        # Normalize titles here until they become available from the index
        norm_title_str = []
        for t in doc.get('title_str'):
            norm_title_str.append(self.normalize(t))
        self.norm_title_str = norm_title_str


    def normalize(self, ne):
        if ne.find('.') > -1:
            ne = ne.replace('.', ' ')
        if ne.find('-') > -1:
            ne = ne.replace('-', ' ')
        if ne.find(u'\u2013') > -1:
            ne = ne.replace(u'\u2013', ' ')
        while ne.find('  ') > -1:
            ne = ne.replace('  ', ' ')
        ne = ne.strip()
        ne = ne.lower()
        return ne


class Entity():

    orig_ne = ''
    ne_type = ''
    url = ''

    clean_ne = ''
    norm_ne = ''
    ne = ''
    last_part = ''

    titles = []

    ocr = ''
    publ_date = ''
    publ_place = ''

    quotes = 0


    def __init__(self, ne, ne_type=None, url=None):
        self.orig_ne = ne
        self.ne_type = ne_type
        self.url = url

        self.clean_ne = self.clean(ne.decode('utf-8'))
        self.norm_ne = self.normalize(self.clean_ne)
        self.ne, self.titles = self.strip_titles(self.norm_ne)
        self.last_part = self.strip_digits(self.ne)


    def clean(self, ne):
        '''
        Remove unwanted characters from the named entity.
        '''
        remove_char = ["+", "&&", "||", "!", "(", ")", "{", u'„',
                       "}", "[", "]", "^", "\"", "~", "*", "?", ":"]

        for char in remove_char:
            if ne.find(char) > -1:
                ne = ne.replace(char, u'')

        ne = ne.strip()
        return ne


    def normalize(self, ne):
        '''
        Remove periods, hyphens, capitalization.
        '''
        if ne.find('.') > -1:
            ne = ne.replace('.', ' ')
        if ne.find('-') > -1:
            ne = ne.replace('-', ' ')
        if ne.find(u'\u2013') > -1:
            ne = ne.replace(u'\u2013', ' ')
        while ne.find('  ') > -1:
            ne = ne.replace('  ', ' ')
        ne = ne.strip()
        ne = ne.lower()
        return ne


    def strip_titles(self, ne):

        GENERAL_TITLES_MALE = ['de heer', 'dhr']
        GENERAL_TITLES_FEMALE = ['mevrouw', 'mevr', 'mw', 'mejuffrouw', 'juffrouw',
            'mej']
        ACADEMIC_TITLES = ['professor', 'prof', 'drs', 'mr', 'ing', 'ir', 'dr',
            'doctor', 'meester', 'doctorandus', 'ingenieur']
        POLITICAL_TITLES = ['minister', 'minister-president', 'staatssecretaris',
            'ambassadeur', 'kamerlid', 'burgemeester', 'wethouder',
            'gemeenteraadslid', 'consul']
        MILITARY_TITLES = ['generaal', 'gen', 'majoor', 'maj', 'luitenant',
            'kolonel', 'kol', 'kapitein']
        RELIGIOUS_TITLES = ['dominee', 'ds', 'paus', 'kardinaal', 'aartsbisschop',
            'bisschop', 'monseigneur', 'mgr', 'kapelaan', 'deken', 'abt',
            'prior', 'pastoor', 'pater', 'predikant', 'opperrabbijn', 'rabbijn',
            'imam']
        TITLES = (GENERAL_TITLES_MALE + GENERAL_TITLES_FEMALE + ACADEMIC_TITLES +
            POLITICAL_TITLES + MILITARY_TITLES + RELIGIOUS_TITLES)

        titles = []

        for t in TITLES:
            regex = '(^|\W)('+t+')(\W|$)'
            if re.search(regex, ne) is not None:
                titles.append(t)
                ne = re.sub(regex, ' ', ne)
        while ne.find('  ') > -1:
            ne = ne.replace('  ', ' ')
        ne = ne.strip()
        return ne, titles


    def strip_digits(self, ne):
        ne_parts = ne.split()
        last_part = None
        for part in reversed(ne_parts):
            if not part.isdigit():
                last_part = part
                break
        return last_part


    def get_ocr(self):
        '''
        Get plain text OCR for the article in which the ne occurs.
        '''
        url = self.url
        f = urllib.urlopen(url)
        ocr_string = f.read()
        f.close()
        ocr_tree = etree.fromstring(ocr_string)
        self.ocr = etree.tostring(ocr_tree, encoding='utf8', method='text')


    def get_metadata(self):
        '''
        Get the date and place of publication of the article in which the ne occurs.
        '''
        url = self.url
        pos = url.find('mpeg21') + 6
        url = url[:pos]
        f = urllib.urlopen(url)
        md_string = f.read()
        f.close()
        md_tree = etree.fromstring(md_string)
        md_id = url[url.find('ddd:'):] + ':metadata'
        for node in md_tree.iter('{urn:mpeg:mpeg21:2002:02-DIDL-NS}Component'):
            if node.attrib['{http://purl.org/dc/elements/1.1/}identifier'] == md_id:
                dcx = node.find('{urn:mpeg:mpeg21:2002:02-DIDL-NS}Resource/{info:srw/schema/1/dc-v1.1}dcx')
                self.publ_date = dcx.findtext('{http://purl.org/dc/elements/1.1/}date')
                for sp in dcx.iter('{http://purl.org/dc/terms/}spatial'):
                    if '{http://www.w3.org/2001/XMLSchema-instance}type' in sp.attrib:
                        self.publ_place = sp.text


    def count_quotes(self):
        '''
        Count the number of quote characters surrounding occurences of the
        entity in the ocr text.
        '''
        quote_chars = ['"', "'", '„', '”', '‚', '’']
        pos = [m.start() - 1 for m in re.finditer(re.escape(self.orig_ne), self.ocr)]
        pos.extend([m.end() for m in re.finditer(re.escape(self.orig_ne), self.ocr)])

        quotes = 0
        for p in pos:
            if self.ocr[p] in quote_chars:
                quotes += 1
        self.quotes = quotes


if __name__ == '__main__':

    if not len(sys.argv) > 1:
        print("Usage: ./disambiguation.py [Named Entity (string)]")
    else:
        linker = Linker(debug=True)

    if len(sys.argv) > 3:
        print(linker.link(sys.argv[1], sys.argv[2], sys.argv[3]))
    else:
        print(linker.link(sys.argv[1]))

