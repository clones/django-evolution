import copy

from django.conf import settings
from django.contrib.contenttypes import generic
from django.db.models.fields import *
from django.db.models.fields.related import *
from django.db import models
from django_evolution.signature import ATTRIBUTE_DEFAULTS
from django_evolution import CannotSimulate, SimulationFailure

FK_INTEGER_TYPES = ['AutoField', 'PositiveIntegerField', 'PositiveSmallIntegerField']

def get_evolution_module():
    module_name = ['django_evolution.db',settings.DATABASE_ENGINE]
    return __import__('.'.join(module_name),{},{},[''])

def create_field(field_name, field_type, field_attrs):
    """
    Create an instance of a field from a field signature. This is useful for
    accessing all the database property mechanisms built into fields.
    """
    # related_model isn't a valid field attribute, so it must be removed
    # prior to instantiating the field, but it must be restored
    # to keep the signature consistent.
    related_model = field_attrs.pop('related_model', None)
    if related_model:
        related_app_name, related_model_name = related_model.split('.')
        to = models.get_model(related_app_name, related_model_name)
        field = field_type(to, name=field_name, **field_attrs)
        field_attrs['related_model'] = related_model
    else:
        field = field_type(name=field_name, **field_attrs)

    return field

class MockMeta:
    "A mockup of a models Options object, based on the model signature"
    def __init__(self, model_sig):
        self.meta = model_sig['meta']
    def __getattr__(self, name):
        return self.meta[name]
            
class BaseMutation:
    def __init__(self):
        pass
        
    def mutate(self, app_label, proj_sig):
        """
        Performs the mutation on the database. Database changes will occur 
        after this function is invoked.
        """
        raise NotImplementedError()
    
    def simulate(self, app_label, proj_sig):
        """
        Performs a simulation of the mutation to be performed. The purpose of
        the simulate function is to ensure that after all mutations have occured
        the database will emerge in a state consistent with the currently loaded
        models file.
        """
        raise NotImplementedError()
        
class SQLMutation(BaseMutation):
    def __init__(self, tag, sql, update_func=None):
        self.tag = tag
        self.sql = sql
        self.update_func = update_func
        
    def __str__(self):
        return "SQLMutation('%s')" % self.tag
    
    def simulate(self, app_label, proj_sig):    
        "SQL mutations cannot be simulated unless an update function is provided"
        if callable(self.update_func):
            self.update_func(app_label, proj_sig)
        else:
            raise CannotSimulate('Cannot simulate SQLMutations')

    def mutate(self, app_label, proj_sig):
        "The mutation of an SQL mutation returns the raw SQL"
        return self.sql
        
class DeleteField(BaseMutation):
    def __init__(self, model_name, field_name):
        self.model_name = model_name
        self.field_name = field_name
    
    def __str__(self):
        return "DeleteField('%s', '%s')" % (self.model_name, self.field_name)
        
    def simulate(self, app_label, proj_sig):    
        app_sig = proj_sig[app_label]
        model_sig = app_sig[self.model_name]

        # If the field was used in the unique_together attribute, update it.
        unique_together = model_sig['meta']['unique_together']
        unique_together_list = [] 
        for ut_index in range(0, len(unique_together), 1):
            ut = unique_together[ut_index]
            unique_together_fields = []
            for field_name_index in range(0, len(ut), 1):
                field_name = ut[field_name_index]
                if not field_name == self.field_name:
                    unique_together_fields.append(field_name)
                    
            unique_together_list.append(tuple(unique_together_fields))
        model_sig['meta']['unique_together'] = tuple(unique_together_list)

        if model_sig['fields'][self.field_name].get('primary_key',False):
            raise SimulationFailure('Cannot delete a primary key.')
        
        # Simulate the deletion of the field.
        try:
            field_sig = model_sig['fields'].pop(self.field_name)
        except KeyError, ke:
            raise SimulationFailure('Cannot find the field named "%s".' % self.field_name)
            
    def mutate(self, app_label, proj_sig):
        app_sig = proj_sig[app_label]
        model_sig = app_sig[self.model_name]
        field_sig = model_sig['fields'][self.field_name]

        # Temporarily remove field_type from the field signature 
        # so that we can create a field
        field_type = field_sig.pop('field_type')
        field = create_field(self.field_name, field_type, field_sig)
        field_sig['field_type'] = field_type
        
        if field_type == models.ManyToManyField:
            opts = MockMeta(model_sig)
            m2m_table = field._get_m2m_db_table(opts)
            sql_statements = get_evolution_module().delete_table(m2m_table)
        else:            
            table_name = app_sig[self.model_name]['meta'].get('db_table')
            sql_statements = get_evolution_module().delete_column(table_name, field)
            
        return sql_statements
        
class AddField(BaseMutation):
    def __init__(self, model_name, field_name, field_type, **kwargs):
        self.model_name = model_name
        self.field_name = field_name
        self.field_type = field_type
        self.field_attrs = kwargs        
                
    def __str__(self):
        params = (self.model_name, self.field_name, self.field_type.__name__)
        str_output = ["'%s', '%s', models.%s" % params]
        for key,value in self.field_attrs.items():
            if isinstance(value, str):
                str_output.append("%s='%s'" % (key,value))
            else:
                str_output.append("%s=%s" % (key,value))
        return 'AddField(' + ', '.join(str_output) + ')'

    def simulate(self, app_label, proj_sig):    
        app_sig = proj_sig[app_label]
        model_sig = app_sig[self.model_name]        
                
        if self.field_type != models.ManyToManyField:
            if not self.field_attrs.get('null', False):
                raise SimulationFailure("Cannot create new column '%s' on '%s.%s' that prohibits null values" % (
                        self.field_name, app_label, self.model_name))
        
        if self.field_name in model_sig['fields']:
            raise SimulationFailure("Model '%s.%s' already has a field named '%s'" % (
                        app_label, self.model_name, self.field_name))
            
        model_sig['fields'][self.field_name] = {
            'field_type': self.field_type,
        }
        model_sig['fields'][self.field_name].update(self.field_attrs)

    def mutate(self, app_label, proj_sig):
        if self.field_type == models.ManyToManyField:
            return self.add_m2m_table(app_label, proj_sig)
        else:
            return self.add_column(app_label, proj_sig)
    
    def add_column(self, app_label, proj_sig):
        app_sig = proj_sig[app_label]
        model_sig = app_sig[self.model_name]

        field = create_field(self.field_name, self.field_type, self.field_attrs)

        sql_statements = get_evolution_module().add_column(model_sig['meta']['db_table'], field)
                                               
        # Create SQL index if necessary
        if self.field_attrs.get('db_index', False):
            sql_statements.extend(get_evolution_module().create_index(
                                        model_sig['meta']['db_table'], 
                                        model_sig['meta'].get('db_tablespace',None), 
                                        field))

        return sql_statements

    def add_m2m_table(self, app_label, proj_sig):
        app_sig = proj_sig[app_label]
        model_sig = app_sig[self.model_name]
        
        opts = MockMeta(model_sig)
        field = create_field(self.field_name, self.field_type, self.field_attrs)
        m2m_table = field._get_m2m_db_table(opts)

        # If this is an m2m relation to self, avoid the inevitable name clash
        related_model = self.field_attrs['related_model']
        related_app_name, related_model_name = related_model.split('.')
        if '.'.join([app_label, self.model_name]) == related_model:
            m2m_column_name = 'from_' + self.model_name.lower() + '_id'
            m2m_reverse_name = 'to_' + related_model_name.lower() + '_id'
        else:
            m2m_column_name = self.model_name.lower() + '_id'
            m2m_reverse_name = related_model_name.lower() + '_id'
    
        model_tablespace = model_sig['meta']['db_tablespace']
        if self.field_attrs.has_key('db_tablespace'):
            field_tablespace = self.field_attrs['db_tablespace']
        else:
            field_tablespace = ATTRIBUTE_DEFAULTS['db_tablespace']
        
        auto_field_db_type = models.AutoField(primary_key=True).db_type()
        if self.field_type in FK_INTEGER_TYPES:
            fk_db_type = models.IntegerField().db_type()
        else:
            # TODO: Fix me
            fk_db_type = models.IntegerField().db_type()
            #fk_db_type = getattr(models,self.field_type)(**self.field_attrs).db_type()

        model_table = model_sig['meta']['db_table']
        model_pk_column = model_sig['meta']['pk_column']

        rel_model_sig = app_sig[related_model_name]
        
        # Refer to the way that sql_all creates the necessary sql to create the table.
        # It requires the data types of the column that it is related to. How we get this
        # in our context is unclear.
        
#        rel_model_pk_col = rel_model_sig['meta']['pk_column']
#        rel_field_sig = rel_model_sig['fields'][rel_model_pk_col]
        # FIXME
#        if rel_field_sig['field_type'] in FK_INTEGER_TYPES:
        rel_fk_db_type = models.IntegerField().db_type()
#        else:

#            rel_fk_db_type = getattr(models,rel_field_sig['field_type'])(**rel_field_sig).db_type()
        
        rel_db_table = rel_model_sig['meta']['db_table']
        rel_pk_column = rel_model_sig['meta']['pk_column']

        sql_statements = get_evolution_module().add_table(
                            model_tablespace, field_tablespace,
                            m2m_table, auto_field_db_type,
                            m2m_column_name, m2m_reverse_name,
                            fk_db_type, model_table, model_pk_column,
                            rel_fk_db_type, rel_db_table, rel_pk_column)
                            
        return sql_statements

class RenameField(BaseMutation):
    def __init__(self, model_name, old_field_name, new_field_name, 
                 new_db_column=None, new_db_table=None):
        self.model_name = model_name
        self.old_field_name = old_field_name
        self.new_field_name = new_field_name
        self.new_db_column = new_db_column
        self.new_db_table = new_db_table
        
    def __str__(self):
        params = "'%s', '%s', '%s'" % (self.model_name, self.old_field_name, self.new_field_name)
        
        if self.new_db_column:
            params = params + ", new_db_column='%s'" % (self.new_db_column)
        if self.new_db_table:
            params = params + ", new_db_table='%s'" % (self.new_db_table)
        
        return "RenameField(%s)" % params
        
    def simulate(self, app_label, proj_sig):
        app_sig = proj_sig[app_label]
        model_sig = app_sig[self.model_name]        
        field_dict = model_sig['fields']
        field_sig = field_dict[self.old_field_name]
        
        if models.ManyToManyField == field_sig['field_type'] and self.new_db_table:
            field_sig['db_table'] = self.new_db_table
        # FIXME! This should be stored as part of the simulate change
        # else:
        #     field_sig['db_column'] = self.new_db_column
        field_dict[self.new_field_name] = field_dict.pop(self.old_field_name)
        
    def mutate(self, app_label, proj_sig):
        app_sig = proj_sig[app_label]
        model_sig = app_sig[self.model_name]        
        old_field_sig = model_sig['fields'][self.old_field_name]

        # Temporarily remove the field type so that we can create mock field instances
        field_type = old_field_sig.pop('field_type')
        # Duplicate the old field sig, and apply the table/column changes
        new_field_sig = copy.copy(old_field_sig)
        if models.ManyToManyField == field_type:
            new_field_sig['db_table'] = self.new_db_table
        new_field_sig['db_column'] = self.new_db_column

        # Create the mock field instances.
        old_field = create_field(self.old_field_name, field_type, old_field_sig)
        new_field = create_field(self.new_field_name, field_type, new_field_sig)
        
        # Restore the field type to the signature
        old_field_sig['field_type'] = field_type
        
        if models.ManyToManyField == field_type:
            opts = MockMeta(model_sig)
            old_m2m_table = old_field._get_m2m_db_table(opts)
            new_m2m_table = new_field._get_m2m_db_table(opts)
            
            return get_evolution_module().rename_table(old_m2m_table, new_m2m_table)
        else:
            table_name = table_name = model_sig['meta']['db_table']
            attname, old_column = old_field.get_attname_column()
            attname, new_column = new_field.get_attname_column()

            return get_evolution_module().rename_column(table_name, old_column, new_column)
