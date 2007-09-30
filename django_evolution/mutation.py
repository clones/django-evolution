from django.conf import settings
from django.db.models.fields import *
from django.db.models.fields.related import *
from django_evolution import EvolutionException, CannotSimulate
import copy

def get_evolution_module():
    module_name = ['django_evolution.db',settings.DATABASE_ENGINE]
    return __import__('.'.join(module_name),{},{},[''])

class BaseMutation:
    def __init__(self):
        pass
        
    def pre_mutate(self, app_sig):
        """
        Invoked before the mutate function is invoked. This function is a stub
        to be overridden by subclasses if necessary.
        """
        pass
    
    def post_mutate(self):
        """
        Invoked after the mutate function is invoked. This function is a stub
        to be overridden by subclasses if necessary.
        """
        pass
    
    def mutate(self, app_sig):
        """
        Performs the mutation on the database. Database changes will occur 
        after this function is invoked.
        """
        raise NotImplementedError()
    
    def pre_simulate(self):
        """
        Invoked before the simulate function is invoked. This function is a stub
        to be overridden by subclasses if necessary.
        """
        pass
    
    def post_simulate(self):
        """
        Invoked after the simulate function is invoked. This function is a stub
        to be overridden by subclasses if necessary.
        """
        pass
    
    def simulate(self, app_sig):
        """
        Performs a simulation of the mutation to be performed. The purpose of
        the simulate function is to ensure that after all mutations have occured
        the database will emerge in a state consistent with the currently loaded
        models file.
        """
        raise NotImplementedError()
        
class SQLMutation(BaseMutation):
    def __init__(self, sql):
        self.sql = sql

    def __str__(self):
        return "SQLMutation()"
        
    def mutate(self, app_sig):
        "The mutation of an SQL mutation returns the raw SQL"
        return self.sql
    
    def simulate(self, app_sig):
        "SQL mutations cannot be simulated"
        raise CannotSimulate()
        
class DeleteField(BaseMutation):
    def __init__(self, model_class, field_name):
        self.model_class = model_class
        self.field_name = str(field_name)
    
    def __str__(self):
        return "DeleteField(%s, '%s')" % (self.model_class._meta.object_name, self.field_name)
        
    def simulate(self, app_sig):
        model_sig = app_sig[self.model_class._meta.object_name]

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
        
        # Update the list of column names
        field_sig = model_sig['fields'][self.field_name]
        if not 'ManyToManyField' == field_sig['internal_type']:
            column_name = field_sig['column']

        # Simulate the deletion of the field.
        try:
            if model_sig['fields'][self.field_name]['primary_key']:
                print 'Primary key deletion is not supported.'
                return
            else:
                field_sig = model_sig['fields'].pop(self.field_name)
        except KeyError, ke:
            print 'SIMULATE ERROR: Cannot find the field named "%s".' % self.field_name
            
    def pre_mutate(self, app_sig):
        model_sig = app_sig[self.model_class._meta.object_name]
        try:
            field_sig = model_sig['fields'][self.field_name]
            internal_type = field_sig['internal_type']
            if internal_type == 'ManyToManyField':
                # Deletion of the many to many field involve dropping a table
                self.manytomanytable = field_sig['m2m_db_table']
                self.mutate_func = self.mutate_table
            else:
                self.column_name = field_sig['column']
                self.mutate_func = self.mutate_column
        except KeyError, ke:
            raise EvolutionException('Pre-Mutate Error: Cannot find the field called "%s".'%self.field_name)
            
    def mutate(self, app_sig):
        evo_module = get_evolution_module()
        return self.mutate_func(evo_module, app_sig)
        
    def mutate_column(self, evo_module, app_sig):
        table_name = self.model_class._meta.db_table
        sql_statements = evo_module.delete_column(app_sig, 
                                                  table_name, 
                                                  self.column_name)
        table_data = app_sig[self.model_class._meta.object_name]                                                  
        return sql_statements
        
    def mutate_table(self, evo_module, app_sig):
        sql_statements = evo_module.delete_table(app_sig, self.manytomanytable)
        table_data = app_sig.pop(self.model_class._meta.object_name)
        return sql_statements
        
class RenameField(BaseMutation):
    def __init__(self, model_class, old_field_name, new_field_name):
        self.model_class = model_class
        self.old_field_name = str(old_field_name)
        self.new_field_name = str(new_field_name)
        
    def __str__(self):
        return "RenameField(%s, '%s', '%s')" % (self.model_class._meta.object_name, self.old_field_name, self.new_field_name)
        
    def simulate(self, app_sig):
        model_sig = app_sig[self.model_class._meta.object_name]

        # If the field was used in the unique_together attribute, update it.
        unique_together = model_sig['meta']['unique_together']
        unique_together_list = [] 
        for ut_index in range(0, len(unique_together), 1):
            ut = unique_together[ut_index]
            unique_together_fields = []
            for field_name_index in range(0, len(ut), 1):
                field_name = ut[field_name_index]
                if field_name == self.old_field_name:
                    unique_together_fields.append(self.new_field_name)
                else:
                    unique_together_fields.append(field_name)
            unique_together_list.append(tuple(unique_together_fields))
        model_sig['meta']['unique_together'] = tuple(unique_together_list)
        
        # Update the column names
        field = self.model_class._meta.get_field(self.new_field_name)
        field_sig = model_sig['fields'][self.old_field_name]
        if not isinstance(field,(ManyToManyField)):
            old_column_name = field_sig['column']
            new_column_name = field.column
            
        # Simulate the renaming of the field.
        try:
            field = self.model_class._meta.get_field(self.new_field_name)
            field_sig = model_sig['fields'].pop(self.old_field_name)
            if isinstance(field,(ManyToManyField)):
                # Many to Many fields involve the renaming of a database table.
                field_sig['m2m_db_table'] = field.m2m_db_table()
            else:
                # All other fields involve renaming of a column only.
                field_sig['column'] = field.column
            field_sig[field.name] = field_sig
                
        except KeyError, ke:
            print 'ERROR: Cannot find the field named "%s".'%self.old_field_name
            
    def pre_mutate(self, app_sig):
        model_sig = app_sig[self.model_class._meta.object_name]
        try:
            field = self.model_class._meta.get_field(self.new_field_name)
            field_sig = model_sig['fields'][self.old_field_name]
            if isinstance(field,(ManyToManyField)):
                # Many to Many fields involve the renaming of a database table.
                self.mutate_func = self.mutate_table
                self.old_table_name = field_sig['m2m_db_table']
                self.new_table_name = field.m2m_db_table()
            else:
                # All other fields involve renaming of a column only.
                self.mutate_func = self.mutate_column
                self.old_column_name = field_sig['column']
                self.new_column_name = field.column
        except KeyError, ke:
            print 'ERROR: Cannot find the field named "%s".'%self.old_field_name
            
    def mutate(self, app_sig):
        evo_module = get_evolution_module()
        return self.mutate_func(evo_module, app_sig)
        
    def mutate_table(self, evo_module, app_sig):
        return evo_module.rename_table(app_sig, 
                                       self.old_table_name, 
                                       self.new_table_name,)

    def mutate_column(self, evo_module, app_sig):
        table_data = app_sig[self.model_class._meta.object_name]
        sql_statements = evo_module.rename_column(app_sig,
                                                  self.model_class._meta.db_table,
                                                  self.old_column_name,
                                                  self.new_column_name,)
        return sql_statements

