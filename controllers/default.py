import logging
import json
from google.appengine.api import urlfetch
from google.appengine.api import memcache
import urllib
import base64
import hashlib
import hmac
import webapp2
import datetime
try:
    import collections
except ImportError:
    logging.exception('Unable to import collections')
try:
    import operator
except ImportError:
    logging.exception('Unable to import operator')
try:
    import xml.etree.cElementTree as etree
except ImportError:
    import xml.etree.ElementTree as etree

# Import local modules
from controllers import utils
from controllers import datastore
from models import models

class MainHandler(utils.BaseHandler):
    def get(self):
        self.set_request_arguments()
        dvla_data = None
        transaction_type = self.experian_config['transaction_type']
        try:
            if 'vrm' in self.context['request_args'] and self.context['request_args']['vrm'] != '':

                is_valid_vrm = utils.validate_vrm(self.context['request_args']['vrm'])
                if is_valid_vrm is None:
                    raise Exception('Invalid VRM value')
                memcache_key = utils.create_memcache_key('willitfitinmycar', **self.context['request_args'])
                logging.info('Request : memcache_key')
                logging.info(memcache_key)
                memcache_response = memcache.get(memcache_key)

                if memcache_response is not None:
                    self.content = memcache_response
                else:

                    memcache_vrm_key = 'vrm:'+self.context['request_args']['vrm']
                    memcache_vrm_response = memcache.get(memcache_vrm_key)
                    if memcache_vrm_response is not None:
                        dvla_data = memcache_vrm_response
                        logging.info('Getting VRM Cara Data from Memcache')
                    else:

                        if 'transactionType' in self.context['request_args'] and self.context['request_args']['transactionType'] != '':
                            transaction_type = self.context['request_args']['transactionType']
                    
                        payload = '<EXPERIAN><ESERIES><FORM_ID>B2INT</FORM_ID></ESERIES><MXIN><TRANSACTIONTYPE>'+transaction_type+'</TRANSACTIONTYPE><PAYMENTCOLLECTIONTYPE>02</PAYMENTCOLLECTIONTYPE><USERNAME>'+self.experian_config['username']+'</USERNAME><PASSWORD>'+self.experian_config['password']+'</PASSWORD><VRM>'+self.context['request_args']['vrm']+'</VRM></MXIN></EXPERIAN>'
                    
                        response = urlfetch.fetch(
                            url=self.experian_config['url'],
                            method='POST',
                            payload=payload,
                            deadline=30,
                            headers=self.urlfetch['headers']
                        )
                        if response.status_code == 200:
                            dvla_data = response.content
                            memcache.set(memcache_vrm_key, value=dvla_data, time=1*60*60*24)
                            logging.info('Getting VRM Car Data from DVLA')
                        else:
                            logging.exception(response.content)
                            raise Exception('Bad Experian Response')

                    # Then...

                    # Create a Vehicle dictionary                        
                    json_response = dict()

                    # Add the request args back into the response object
                    json_response['request_args'] = self.context['request_args']
                    json_response['product_packaging'] = self.product_packaging
                    json_response['unit_of_measurement'] = self.unit_of_measurement

                    json_vehicle_data = self.create_json_response(dvla_data)
                    if json_vehicle_data is None:
                    	raise Exception('Unable to parse DVLA vehicle data')
                    
                    logging.info(json_vehicle_data)

                    # Add the DVLA vehicle data or error
                    json_response['dvla'] = json_vehicle_data
                    
                    # Get Make
                    make = None
                    if json_vehicle_data.get('MAKE') is not None:
                        make = json_vehicle_data.get('MAKE').lower()
                        make = make.title()
                        logging.info(make)
                    
                    # Get Model
                    model = None
                    if json_vehicle_data.get('MODEL') is not None:
                        model = json_vehicle_data.get('MODEL').lower()
                        model = model.title()
                        # Now select the first word of the model, as we will use this to match our Datastore models
                        model = model.split(' ')[0]
                        logging.info(model)

                    # Get Year Of Manufacture
                    year_of_manufacture = json_vehicle_data.get('YEAROFMANUFACTURE')
                    logging.info(year_of_manufacture)

                    # Get Door Plan Literal
                    door_plan_literal = ''
                    door_plan_literal_string = None
                    if json_vehicle_data.get('DOORPLANLITERAL') is not None:
                        door_plan_literal_string = json_vehicle_data.get('DOORPLANLITERAL')
                    
                    if door_plan_literal_string is not None:
                        door_plan_literal_string = door_plan_literal_string.lower()
                        door_plan_literal_string = door_plan_literal_string.title()

                    if door_plan_literal_string in models.dvla_door_plan_literal_inv:
                        door_plan_literal = models.dvla_door_plan_literal_inv.get(door_plan_literal_string)

                    logging.info(door_plan_literal)

                    # Query the DB for the Vehicle
                    try:
                        query = datastore.get_vehicle(**dict(
                            make=make,
                            model=model,
                            door_plan_literal=door_plan_literal,
                            year_of_manufacture=year_of_manufacture
                        ))

                        
                        # Add the DB Query data
                        json_response['datastore'] = query
                        if query is not None:
                            json_response['datastore']['door_plan_literal_string'] = door_plan_literal_string
                            # Calculate the Product fit and add this to the JSON response object
                            product_fit_score = self.calculate_vehicle_fit(query, self.context['request_args'])
                            json_response['score'] = product_fit_score

                        # Cache for longevity
                        memcache.set(memcache_key, json_response)
                    except Exception, e:
                        # Add the DB Query error
                        json_response['datastore'] = str(e)
                        # Cache for a short period
                        memcache.set(memcache_key, json_response, time=8000)        

                    # Set the response context data
                    self.content = dict(data=json_response)
                    logging.info(json_response)

                    
            else:
                raise Exception('VRM parameter value missing')
                
        except Exception, e:
            logging.exception(e)
            self.set_response_error(e.message, 500)
        finally:
            self.function_name = self.request.get(self.jsonp_request_arg)
            if self.function_name is not None and self.function_name != '':
                self.render_jsonp()
            else:
                self.render_json()

    """
    	@name: calculate_vehicle_fit
    	@description:
    		Calculate whether a product will fit in the boot of the car.
    		Get the minimum width of the boot aperture, and compare this with product width
    		Get the height of the boot aperture, and compare this with product height
    		Get the length of the boot, and compare this with product length
    """
    def calculate_vehicle_fit(self, json_vehicle, request_args):
    	try:
            boot_aperture_width_bottom = float(json_vehicle.get('boot_aperture_width_bottom'))
            boot_aperture_width_middle = float(json_vehicle.get('boot_aperture_width_middle'))
            boot_aperture_width_top = float(json_vehicle.get('boot_aperture_width_top'))
            boot_aperture_verticalheight = float(json_vehicle.get('boot_aperture_verticalheight'))
            boot_length = float(json_vehicle.get('boot_length'))
            product_fit_score = dict(width=None, height=None, length=None)
            boot_widths = [boot_aperture_width_bottom, boot_aperture_width_middle, boot_aperture_width_top]
            boot_minimum_width = min(float(w) for w in boot_widths)
            # Create a dictionary of Vehicle dimensions
            vehicle_dims = dict(length=boot_length, height=boot_aperture_verticalheight, width=boot_minimum_width)
            # Sort Vehicle dimensions in descending order
            sorted_vehicle_dims = sorted(vehicle_dims.iteritems(), key=operator.itemgetter(1), reverse=True)
            
            # Sort Product dimensions in descending order
            product_width = float(request_args.get('width'))
            product_width = product_width+(product_width/100)*self.product_packaging
            product_height = float(request_args.get('height'))
            product_height = product_height+(product_height/100)*self.product_packaging
            product_length = float(request_args.get('length'))
            product_length = product_length+(product_length/100)*self.product_packaging
            product_dims = dict(
                width=product_width,
                height=product_height,
                length=product_length
            )

            sorted_product_dims = sorted(product_dims.iteritems(), key=operator.itemgetter(1), reverse=True)
            
            counter = 0
            for vehicle_dim in sorted_vehicle_dims:
                """
                if vehicle_dim[1] > sorted_product_dims[counter][1]:
                    logging.info('Vehicle dimension '+str(vehicle_dim[0])+' is greater than Product dimension '+str(sorted_product_dims[counter][0]))
                    product_fit_score[vehicle_dim[0]] = 'yes'
                elif vehicle_dim[1] > sorted_product_dims[counter][1]+:
                    logging.info('Vehicle dimension '+str(vehicle_dim[0])+' is equal to Product dimension '+str(sorted_product_dims[counter][0]))
                    product_fit_score[vehicle_dim[0]] = 'maybe'
                elif vehicle_dim[1] < sorted_product_dims[counter][1]:
                    logging.info('Vehicle dimension '+str(vehicle_dim[0])+' is less than Product dimension '+str(sorted_product_dims[counter][0]))
                    product_fit_score[vehicle_dim[0]] = 'no'                    
                """
                # 'Yes' scenario
                if sorted_product_dims[counter][1] < (vehicle_dim[1]-self.vehicle_boot_space_deviation):
                    logging.info('Product dimension '+str(sorted_product_dims[counter][0])+' is less than Vehicle dimension '+str(vehicle_dim[0]))
                    product_fit_score[vehicle_dim[0]] = 'yes'
                # 'Maybe' scenario
                elif sorted_product_dims[counter][1] > (vehicle_dim[1]-self.vehicle_boot_space_deviation) and sorted_product_dims[counter][1] < vehicle_dim[1]:
                    logging.info('Product dimension '+str(sorted_product_dims[counter][0])+' is about equal to Vehicle dimension '+str(vehicle_dim[0]))
                    product_fit_score[vehicle_dim[0]] = 'maybe'
                # 'No' scenario
                elif sorted_product_dims[counter][1] > vehicle_dim[1]:
                    logging.info('Product dimension '+str(sorted_product_dims[counter][0])+' is greater than Vehicle dimension '+str(vehicle_dim[0]))
                    product_fit_score[vehicle_dim[0]] = 'no'

                counter=counter+1

            return product_fit_score

    	except Exception, e:
    		logging.exception(e)
    		raise e
    	
    def create_json_response(self, xml_content):
        """

        Failure:
        <?xml version='1.0' standalone='yes'?>
        <GEODS exp_cid='jmny'>
            <REQUEST type='RETURN' subtype='CALLBUR' EXP_ExperianRef='' success='N' timestamp='Wed, 3 Apr 2013 at 6:10 PM' id='jmny'>
                <MXE1>
                    <CODE>0009</CODE>
                    <LENGTH>029</LENGTH>
                    <SEVERITY>4</SEVERITY>
                    <MSG>VEHICLE NOT FOUND ON DATABASE</MSG>
                </MXE1>
            </REQUEST>
        </GEODS>

        Failure:
        <?xml version='1.0' standalone='yes'?>
        <GEODS>
            <REQUEST type='RETURN' success='N'>
                <ERR1>
                    <CODE>TM01</CODE>
                    <SEVERITY>4</SEVERITY>
                    <MESSAGE>The length of the supplied field is invalid(MXIN_VRM 0)</MESSAGE>
                </ERR1>
            </REQUEST>
        </GEODS>


        Success:
        <?xml version='1.0' standalone='yes'?>
        <GEODS exp_cid='86sq'>
            <REQUEST type='RETURN' subtype='CALLBUR' EXP_ExperianRef='' success='Y' timestamp='Tue, 2 Apr 2013 at 1:40 PM' id='86sq'>
                <MB01 seq='01'>
                    <DATEOFTRANSACTION>20120813</DATEOFTRANSACTION>
                    <VRM>P874OPP </VRM>
                    <VINCONFIRMATIONFLAG>0</VINCONFIRMATIONFLAG>
                    <ENGINECAPACITY>01392</ENGINECAPACITY>
                    <DOORPLAN>14</DOORPLAN>
                    <DATEFIRSTREGISTERED>19961030</DATEFIRSTREGISTERED>
                    <YEAROFMANUFACTURE>1996</YEAROFMANUFACTURE>
                    <SCRAPPED>0</SCRAPPED>
                    <EXPORTED>0</EXPORTED>
                    <IMPORTED>0</IMPORTED>
                    <MAKE>NISSAN</MAKE>
                    <MODEL>ALMERA EQUATION</MODEL>
                    <COLOUR>GREEN</COLOUR>
                    <TRANSMISSION>MANUAL 5 GEARS</TRANSMISSION>
                    <ENGINENUMBER>GA14-202726</ENGINENUMBER>
                    <VINSERIALNUMBER>JN1FAAN15U0013185</VINSERIALNUMBER>
                    <DOORPLANLITERAL>5 DOOR HATCHBACK</DOORPLANLITERAL>
                    <MVRISMAKECODE>S8</MVRISMAKECODE>
                    <MVRISMODELCODE>TBB</MVRISMODELCODE>
                    <DTPMAKECODE>RA</DTPMAKECODE>
                    <DTPMODELCODE>289</DTPMODELCODE>
                    <TRANSMISSIONCODE>M</TRANSMISSIONCODE>
                    <GEARS>5</GEARS>
                    <FUEL>PETROL</FUEL>
                    <CO2EMISSIONS>*</CO2EMISSIONS>
                    <USEDBEFORE1STREG>0</USEDBEFORE1STREG>
                    <IMPORTNONEU>0</IMPORTNONEU>
                    <UKDATEFIRSTREGISTERED>19961030</UKDATEFIRSTREGISTERED>
                    <MAKEMODEL>NISSAN ALMERA EQUATION</MAKEMODEL>
                </MB01>
                <MB37 seq='01'>
                    <V5CDATACOUNT>01</V5CDATACOUNT>
                    <V5CDATAITEMS>
                        <DATE>20111021</DATE>
                    </V5CDATAITEMS>
                </MB37>
            </REQUEST>
        </GEODS>
        """        
        try:
        	response = dict()
        	root = etree.fromstring(xml_content)
        	request = root.find('REQUEST')
        	success = request.get('success')
        	if success is not None and success == 'Y':
        		mb01 = root.find('REQUEST/MB01')
        		for item in mb01:
        			response[item.tag] = item.text
        	else:
        		error = root.find('REQUEST/ERR1/MESSAGE')
        		if error is not None and error != '':
        			response['error'] = error.text
        		else:
        			error = root.find('REQUEST/MXE1/MSG')
        			if error is not None and error != '':
        				response['error'] = error.text
        	return response
        except Exception, e:
        	logging.exception(e)
        	raise e