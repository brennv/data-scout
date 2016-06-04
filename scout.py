import os
import time
import random
import requests
import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from tqdm import tqdm
import json
import slackweb


class Scout():
    def __init__(self, doc):
        self.domain = doc['domain']
        self.kind = doc['kind']
        self.name = doc['name']
        self.locale = doc['locale']
        self.link = doc['link']
        self.db = doc['db']
        self.slackUrl = doc['slackUrl']

    def go(self):

        def get_json(self, endpoint):
            '''Ping endpoint and return json.'''
            url = 'https://' + self.domain + endpoint
            # self.domain_token = None  # TODO
            # if self.domain_token:
            #    url = url + '&$$app_token=' + self.domain_token
            try:
                json = requests.get(url).json()
            except MaxRetryError as e:
                print('MaxRetryError')
                time.sleep(4000)
            time.sleep(random.uniform(1, 3))
            return json

        def get_keys(self):
            '''Get the ids of all the datasets in the domain catalog.'''
            catalog = get_json(self, '/api/catalog/v1?only=datasets')
            resultCount = catalog['resultSetSize']
            uids = [x['resource']['id'] for x in catalog['results']]
            pages = resultCount // 100 + (resultCount % 100 > 0)
            offset = 0
            for page in range(1, pages):
                offset += 100
                catalog = get_json(self, '/api/catalog/v1?only=datasets&offset=' + str(offset))
                pageUids = [x['resource']['id'] for x in catalog['results']]
                uids = uids + pageUids
            return uids

        def get_metadata(self, uid):
            '''Get metdata for a given dataset unique id.'''
            views = get_json(self, '/api/views/' + uid + '.json')
            name = views['name']
            updated = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(views['rowsUpdatedAt']))
            columns = [x['name'] for x in views['columns']]
            columnCount = len(columns)
            try:
                blurb = views['description']
            except KeyError:
                blurb = ''
            try:
                resource = get_json(self, '/resource/' + uid + '.json?$select=count(*)')
                rowCount = int(resource[0]['count'])
            except KeyError:
                rowCount = 0
            return name, updated, rowCount, columnCount, columns, blurb

        def get_df(self):
            '''Combine dataset metadatas into a dataframe.'''
            uids = get_keys(self)
            # uids = uids[0:4]  # testing
            names, times, rowCounts, columnCounts, columnNames, blurbs = [], [], [], [], [], []
            for uid in tqdm(uids):
                name, updated, rowCount, columnCount, columns, blurb = get_metadata(self, uid)
                names.append(name)
                times.append(updated)
                rowCounts.append(rowCount)
                columnCounts.append(columnCount)
                columnNames.append(columns)
                blurbs.append(blurb)
            df = pd.DataFrame()
            df['uid'] = uids
            df['name'] = names
            df['blurb'] = blurbs
            df['time'] = times
            df['rowCount'] = [x for x in rowCounts]
            df['columnCount'] = [x for x in columnCounts]
            df['columns'] = [str(x) for x in columnNames]
            # df = df.set_value(0, 'rowCount', 1240)  # testing
            df = df.set_index('uid').sort_index()
            return df

        def set_db(self, df):
            '''Save dataframe as sqlite table.'''
            engine = create_engine('sqlite:///' + self.db)
            with engine.connect() as conn, conn.begin():
                df.to_sql(self.domain, conn, if_exists='replace')
            return True

        def get_db(self):
            '''Read sqlite table into dataframe.'''
            engine = create_engine('sqlite:///' + self.db)
            with engine.connect() as conn, conn.begin():
                df1 = pd.read_sql_table(self.domain, conn)
                df1 = df1.set_index('uid').sort_index()
            return df1

        def check_diff(self):
            '''Check if stored data is up to date, return dataframes.'''
            df1, df2 = get_db(self), get_df(self)
            is_different = True
            if df2.equals(df1):
                is_diff = False
            return df1, df2, is_different

        def diff_rows(df1, df2):
            '''Diff dataframe rows & find common keys.'''
            df_new, df_old = pd.DataFrame(), pd.DataFrame()
            index1, index2 = list(df1.index), list(df2.index)
            key_additions = [x for x in index2 if x not in index1]
            if key_additions:
                df_new = df2.loc[df2.index.isin(key_additions)]
            key_deletions = [x for x in index1 if x not in index2]
            if key_deletions:
                df_old = df1.loc[df1.index.isin(key_deletions)]
            common_keys = [x for x in index2 if x in index1]
            return df_new, df_old, common_keys

        def diff_cells(df1, df2, common_keys):
            '''Diff the columnCounts.'''
            if common_keys:
                df1 = df1.loc[df1.index.isin(common_keys)]
                df2 = df2.loc[df2.index.isin(common_keys)]
                df1_num = df1._get_numeric_data()
                df2_num = df2._get_numeric_data()
                df_mod = df2_num.subtract(df1_num)
                df_mod = df_mod.drop('columnCount', axis=1)  # long shortcut
                df_mod = df_mod[(df_mod.T != 0).any()]
                df_mod['name'] = [df2[df2.index == x]['name'][0] for x in df_mod.index]
            return df_mod

        def add_row_note(self, df, intro):
            '''Write note for datasets that are added or deleted.'''
            notes = []
            if len(df) > 0:
                df['url'] = ['https://' + self.domain + '/resource/' + x for x in df.index]
                # df['name'] = [x[:60] + (x[60:] and '..') for x for x in df['name']]
                df['url_tag'] = ['<' + x + '|' + y + '>' for x, y in zip(df['url'], df['name'])]
                df['note'] = [intro + ' ' + x for x in df['url_tag']]
                df['blurb'] = [x[:160] + (x[160:] and '..') for x in df['blurb']]
                df['note'] = [x + ' _' + y + '_' if len(y) > 2 else x for x, y in zip(df['note'], df['blurb'])]
                for note in df['note']:
                    notes.append(note)
            if notes:
                notes = ' \n'.join(notes)
            return notes

        def down_sample(items, n):
            '''Randomly down sample long lists.'''
            items = [items[x] for x in sorted(random.sample(list(range(len(items))), n))]
            return items

        def add_cell_note(self, df, add_icon, sub_icon):
            '''Write note for diff cell results.'''
            notes = []
            if len(df) > 0:
                df['url'] = ['https://' + self.domain + '/resource/' + x for x in df.index]
                df['url_tag'] = ['<' + x + '|' + y + '>' for x, y in zip(df['url'], df['name'])]
                df['note'] = [x for x in df['url_tag']]
                df['note'] = [add_icon + x + ' added ' if y > 0 else sub_icon + x + ' deleted ' for x, y in zip(df['note'], df['rowCount'])]
                # df['note'] = [x + ' added ' if y > 0 else x + ' deleted ' for x, y in zip(df['note'], df['rowCount'])]
                df['note'] = [x + str(abs(y)) for x, y in zip(df['note'], df['rowCount'])]
                df['note'] = [x + ' rows' if abs(y) > 1 else x + ' row' for x, y in zip(df['note'], df['rowCount'])]
                df = df.sort_values(by='name')
                for note in df['note']:
                    notes.append(note)
            if notes:
                print('len mod notes', len(notes))
                if len(notes) > 5:
                    trim = len(notes) - 5
                    notes = down_sample(notes, 5)  # TODO add ranking and slicing
                    s = 's'
                    if trim == 1:
                        s = ''
                    notes = notes + ['_..and ' + str(trim) + ' additional <' + self.link + '|' + self.name + 'update' + s + '>_']
                notes = ' \n'.join(notes)
            return notes

        def get_notes(self, df1, df2):
            '''Combine notes into one message string.'''
            df_new, df_dep, common_keys = diff_rows(df1, df2)
            df_mod = diff_cells(df1, df2, common_keys)
            new_notes = add_row_note(self, df_new, ':gift: *New* dataset added:')
            dep_notes = add_row_note(self, df_dep, ':boom: Dataset removed:')
            mod_notes = add_cell_note(self, df_mod, add_icon='*+* ', sub_icon='*-* ')
            slack_note = [x for x in [new_notes, dep_notes, mod_notes] if x]
            slack_note = ' \n'.join(slack_note)
            twitter_note = ''
            if new_notes:
                twitter_note = ' \n'.join(new_notes)
            return slack_note, twitter_note

        def notify_slack(self, report):
            '''Send message to slack channel.'''
            response = 'null'
            if report != []:
                slack = slackweb.Slack(url=self.slackUrl)
                response = slack.notify(text=report)
            return response


        '''Check the domain and report changes.'''
        if os.path.isfile(self.db):
            print('checking domain...')
            df1, df2, is_diff = check_diff(self)
            print('is diff', is_diff)
            if is_diff:
                slack_note, twitter_note = get_notes(self, df1, df2)
                if slack_note:
                    print('slack note', slack_note)
                    response = notify_slack(self, slack_note)
                    if response == 'ok':
                        set_db(self, df2)
                        print('set db')
                if twitter_note:
                    print('tweet')
        else:
            print('creating db...')
            df1 = get_df(self)
            set_db(self, df1)
        return True
