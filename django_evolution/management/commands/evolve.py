from optparse import make_option
import sys
import copy
try:
    import cPickle as pickle
except ImportError:
    import pickle as pickle
    
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db.models import get_apps, get_app, signals
from django.db import connection, transaction

from django_evolution import CannotSimulate, SimulationFailure
from django_evolution.models import Version, Evolution
from django_evolution.signature import create_project_sig
from django_evolution.diff import Diff
from django_evolution.evolve import get_unapplied_evolutions, get_mutations

class Command(BaseCommand):
    option_list = BaseCommand.option_list + (
        make_option('--verbosity', action='store', dest='verbosity', default='1',
            type='choice', choices=['0', '1', '2'],
            help='Verbosity level; 0=minimal output, 1=normal output, 2=all output'),
        make_option('--noinput', action='store_false', dest='interactive', default=True,
            help='Tells Django to NOT prompt the user for input of any kind.'),        
        make_option('--hint', action='store_true', dest='hint', default=False,
            help='Generate an evolution script that would update the app.'),
        make_option('--sql', action='store_true', dest='compile_sql', default=False,
            help='Compile a Django evolution script into SQL.'),
        make_option('-x','--execute', action='store_true', dest='execute', default=False,
            help='Apply the evolution to the database.'),
    )
    help = 'Evolve the models in a Django project.'
    args = '<appname appname ...>'

    requires_model_validation = False

    def handle(self, *app_labels, **options):
        verbosity = int(options['verbosity'])        
        interactive = options['interactive']
        execute = options['execute']
        compile_sql = options['compile_sql']
        hint = options['hint']
        
        # Use the list of all apps, unless app labels are specified.
        if app_labels:
            if execute:
                raise CommandError('Cannot specify an application name when executing evolutions.')
            try:
                app_list = [get_app(app_label) for app_label in app_labels]
            except (ImproperlyConfigured, ImportError), e:
                raise CommandError("%s. Are you sure your INSTALLED_APPS setting is correct?" % e)
        else:
            app_list = get_apps()

        # Iterate over all applications running the mutations
        evolution_required = False
        simulated = True
        sql = []
        new_evolutions = []
        
        current_proj_sig = create_project_sig()
        current_signature = pickle.dumps(current_proj_sig)

        try:
            latest_version = Version.objects.latest('when')
            database_sig = pickle.loads(str(latest_version.signature))
            diff = Diff(database_sig, current_proj_sig)
        except Evolution.DoesNotExist:
            print self.style.ERROR("Can't evolve yet. Need to set an evolution baseline.")
            sys.exit(1)
        
        try:            
            for app in app_list:
                app_label = app.__name__.split('.')[-2]
                if hint:
                    evolutions = []
                    mutations = diff.evolution().get(app_label,[])
                else:
                    evolutions = get_unapplied_evolutions(app)
                    mutations = get_mutations(app, evolutions)
                
                if mutations:
                    evolution_required = True
                    for mutation in mutations:
                        sql.extend(mutation.mutate(app_label, database_sig))
                        try:
                            mutation.simulate(app_label, database_sig)
                        except CannotSimulate:
                            simulated = False
                    new_evolutions.extend(Evolution(app_label=app_label, label=label) 
                                            for label in evolutions)
                    
                    if not execute:
                        if compile_sql:
                            print ';; Compiled evolution SQL for %s' % app_label
                            for s in sql:
                                print s                            
                        else:
                            print '----- Evolution for %s' % app_label
                            print 'from django_evolution.mutations import *'
                            print 'from django.db import models'
                            print 
                            print 'MUTATIONS = ['
                            print '   ',
                            print ',\n    '.join(str(m) for m in mutations)
                            print ']'
                            print '----------------------'

                else:
                    if verbosity > 1:
                        print 'Application %s is up to date' % app_label
        except SimulationFailure,s:
            print self.style.ERROR('Simulation failure: %s' % s)
            sys.exit(1)
            
        if simulated:
            diff = Diff(database_sig, current_proj_sig)
            if not diff.is_empty():
                print self.style.ERROR('Simulation failure: Signatures do not match at end of simulation')
                print diff
                sys.exit(1)
        else:
            print self.style.NOTICE('Evolution could not be simulated, possibly due to raw SQL mutations')

        if evolution_required:
            if execute:
                # Now that we've worked out the mutations required, 
                # and we know they simulate OK, run the evolutions
                if interactive:
                    confirm = raw_input("""
You have requested a database evolution. This will alter tables 
and data currently in the %r database, and may result in 
IRREVERSABLE DATA LOSS. Evolutions should be *thoroughly* reviewed 
prior to execution. 

Are you sure you want to execute the evolutions?

Type 'yes' to continue, or 'no' to cancel: """ % settings.DATABASE_NAME)
                else:
                    confirm = 'yes'
                
                if confirm.lower() == 'yes':
                    # Begin Transaction
                    transaction.enter_transaction_management()
                    transaction.managed(True)
                    cursor = connection.cursor()
                    try:
                        # Perform the SQL
                        for statement in sql:
                            cursor.execute(statement)  
                        
                        # Now update the evolution table
                        version = Version(signature=current_signature)
                        version.save()
                        for evolution in new_evolutions:
                            evolution.version = version
                            evolution.save()
                        
                        transaction.commit()
                    except Exception, ex:
                        transaction.rollback()
                        print self.style.ERROR('Error applying evolution: %s' % str(ex))
                        sys.exit(1)
                    transaction.leave_transaction_management()
                        
                    if verbosity > 0:
                        print 'Evolution successful.'
                else:
                    print self.style.ERROR('Evolution cancelled.')
            elif not compile_sql:
                if verbosity > 0:
                    if simulated:
                        print "Trial evolution successful."
                        print "Run './manage.py evolve --execute' to apply evolution."
        else:
            if verbosity > 0:
                print 'No evolution required.'
